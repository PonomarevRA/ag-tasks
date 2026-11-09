import AppKit

/// Product identity is supplied by the build, never by downloaded release text.
struct Product: Decodable {
    let appId, name, version, binary, bundleId, repository, defaultDataName: String
}

/// A native setup wizard replaces only an inactive application and stores paths outside its bundle.
final class Setup: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var product: Product!
    var fields: [NSTextField] = []
    var background = NSButton(checkboxWithTitle: "Keep running in the menu bar when the window closes", target: nil, action: nil)
    var login = NSButton(checkboxWithTitle: "Start when I sign in", target: nil, action: nil)
    var widgets = NSButton(checkboxWithTitle: "Install request widgets for AG services (macOS 14+)", target: nil, action: nil)
    var install: NSButton!
    var status = NSTextField(wrappingLabelWithString: "Unsigned build. Existing application data is preserved during updates.")
    var previous: [String: Any]?
    let fm = FileManager.default
    var homeDirectory = FileManager.default.homeDirectoryForCurrentUser
    var resourcesDirectory = Bundle.main.resourceURL!
    var runningBundles: () -> [URL] = { NSWorkspace.shared.runningApplications.compactMap { $0.bundleURL } }
    /// Production verifies actual nested code; synthetic transaction tests inject only this boundary.
    var verifySignature: (URL) throws -> Void = { url in
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        process.arguments = ["--verify", "--deep", "--strict", url.path]
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw CocoaError(.fileReadCorruptFile) }
    }

    /// Includes other processes holding resources, not only an app's main executable.
    var openFileOwners: (URL) throws -> Bool = { try Setup.bundleHasOpenFiles($0) }

    /// Bound lsof and capture only owner IDs in disposable files; uncertainty retains the app.
    static func bundleHasOpenFiles(_ path: URL) throws -> Bool {
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("ag-app-owners-\(UUID().uuidString)")
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let output = directory.appendingPathComponent("pids")
        let errors = directory.appendingPathComponent("errors")
        guard fm.createFile(atPath: output.path, contents: nil), fm.createFile(atPath: errors.path, contents: nil) else {
            throw CocoaError(.fileWriteUnknown)
        }
        let outputHandle = try FileHandle(forWritingTo: output)
        let errorHandle = try FileHandle(forWritingTo: errors)
        defer { try? outputHandle.close(); try? errorHandle.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-t", "+D", path.path]
        process.standardOutput = outputHandle
        process.standardError = errorHandle
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        guard finished.wait(timeout: .now() + 15) == .success else {
            process.terminate()
            throw CocoaError(.fileReadUnknown)
        }
        let outputSize = (try fm.attributesOfItem(atPath: output.path)[.size] as? NSNumber)?.intValue ?? -1
        let errorSize = (try fm.attributesOfItem(atPath: errors.path)[.size] as? NSNumber)?.intValue ?? -1
        guard errorSize == 0, [0, 1].contains(process.terminationStatus), outputSize >= 0 else {
            throw CocoaError(.fileReadUnknown)
        }
        return outputSize > 0
    }

    /// Load the bundled manifest before displaying any selectable installation paths.
    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let resource = resourcesDirectory
            product = try JSONDecoder().decode(Product.self, from: Data(contentsOf: resource.appendingPathComponent("desktop-product.json")))
            try loadPreviousInstallation()
            buildWindow()
        } catch { fatalDialog(error.localizedDescription) }
    }

    /// Reject inaccessible, dangling, oversized or incomplete locators instead of choosing another store.
    func loadPreviousInstallation() throws {
        let path = locator()
        let attributes: [FileAttributeKey: Any]
        do { attributes = try fm.attributesOfItem(atPath: path.path) }
        catch let error as NSError {
            if error.domain == NSCocoaErrorDomain && [NSFileNoSuchFileError, NSFileReadNoSuchFileError].contains(error.code) { return }
            throw error
        }
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue <= 16384 else { throw failure("Unsafe installation settings file.") }
        guard let value = try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as? [String: Any],
              value["schemaVersion"] as? String == "ag-desktop/install-v1", value["appId"] as? String == product.appId else {
            throw failure("Installation settings are invalid; repair them before updating.")
        }
        let old = previous
        previous = value
        do { try validatePreviousInstallation(value) }
        catch { previous = old; throw error }
    }

    /// Keep the stable locator independent from selected artifact and backup directories.
    func locator() -> URL {
        homeDirectory.appendingPathComponent("Library/Application Support/AGDesktop/\(product.appId)/installation.json")
    }

    /// Reuse a known existing per-user/system bundle instead of creating a second installation.
    func existingApplicationPath() -> URL {
        let user = homeDirectory.appendingPathComponent("Applications/\(product.name).app")
        let system = URL(fileURLWithPath: "/Applications/\(product.name).app")
        for candidate in [user, system] {
            guard let data = try? Data(contentsOf: candidate.appendingPathComponent("Contents/Info.plist")),
                  let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else { continue }
            if info["CFBundleIdentifier"] as? String == product.bundleId,
               fm.fileExists(atPath: candidate.appendingPathComponent("Contents/MacOS/\(product.binary)").path) { return candidate }
        }
        return user
    }

    /// Invalid locators cannot silently become a first installation with different storage defaults.
    func validatePreviousInstallation(_ value: [String: Any]) throws {
        var paths: [String] = []
        for field in ["applicationPath", "dataDirectory", "backupDirectory"] {
            guard let path = value[field] as? String else { throw failure("Installation settings are incomplete: \(field)") }
            paths.append(path)
        }
        for field in ["closeToTray", "launchAtLogin"] {
            if let flag = value[field] {
                guard let number = flag as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { throw failure("Invalid installation flag: \(field)") }
            }
        }
        if let port = value["preferredPort"] {
            guard let number = port as? Int, (1024...65535).contains(number) else { throw failure("Invalid preferred port in installation settings.") }
        }
        _ = try validatedPaths(paths)
    }

    /// Use normal AppKit controls with system spacing, keyboard focus, and folder dialogs.
    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 660, height: 430), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Install \(product.name) \(product.version)"
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24), stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24), stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24)])
        let title = NSTextField(labelWithString: product.name)
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        stack.addArrangedSubview(title)
        let home = homeDirectory
        let values = [previous?["applicationPath"] as? String ?? existingApplicationPath().path,
                      previous?["dataDirectory"] as? String ?? home.appendingPathComponent("Library/Application Support/\(product.defaultDataName)").path,
                      previous?["backupDirectory"] as? String ?? home.appendingPathComponent("Library/Application Support/\(product.defaultDataName)-backups").path]
        for (index, label) in ["Application", "Artifacts and data", "Backups"].enumerated() {
            let row = NSStackView(); row.orientation = .horizontal; row.spacing = 8
            let name = NSTextField(labelWithString: label); name.widthAnchor.constraint(equalToConstant: 120).isActive = true
            let field = NSTextField(string: values[index]); field.lineBreakMode = .byTruncatingMiddle
            let browse = NSButton(title: "Choose…", target: self, action: #selector(choose(_:))); browse.tag = index
            row.addArrangedSubview(name); row.addArrangedSubview(field); row.addArrangedSubview(browse)
            stack.addArrangedSubview(row); row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            fields.append(field)
        }
        background.state = (previous?["closeToTray"] as? Bool ?? true) ? .on : .off
        login.state = (previous?["launchAtLogin"] as? Bool ?? false) ? .on : .off
        stack.addArrangedSubview(background); stack.addArrangedSubview(login)
        if fm.fileExists(atPath: resourcesDirectory.appendingPathComponent("Widgets/AG Requests Widgets.app").path) {
            widgets.isEnabled = ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 14
            widgets.state = widgets.isEnabled ? .on : .off
            stack.addArrangedSubview(widgets)
        }
        status.font = .systemFont(ofSize: 12); status.textColor = .secondaryLabelColor
        stack.addArrangedSubview(status)
        install = NSButton(title: "Install", target: self, action: #selector(beginInstall)); install.keyEquivalent = "\r"
        stack.addArrangedSubview(install)
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    /// Select folders explicitly; application selection always appends this product's bundle name.
    @objc func choose(_ sender: NSButton) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            fields[sender.tag].stringValue = sender.tag == 0 ? url.appendingPathComponent("\(product.name).app").path : url.path
        }
    }

    /// Snapshot UI input once, then stage large bundle copies away from AppKit's event loop.
    @objc func beginInstall() {
        let values = fields.map { $0.stringValue }
        let closeToTray = background.state == .on; let launchAtLogin = login.state == .on
        let installWidgets = widgets.state == .on
        install.isEnabled = false; status.stringValue = "Validating paths and staging the application…"
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                try self.performInstall(values, closeToTray: closeToTray, launchAtLogin: launchAtLogin, installWidgets: installWidgets)
                DispatchQueue.main.async {
                    self.status.stringValue = "Installed. Your artifacts and backups remain outside the application."
                    self.install.title = "Open application"; self.install.action = #selector(self.openInstalled); self.install.isEnabled = true
                }
            } catch {
                DispatchQueue.main.async { self.status.stringValue = error.localizedDescription; self.install.isEnabled = true }
            }
        }
    }

    /// Serialize POSIX physical paths so Python/.NET agree with Foundation's /var presentation aliases.
    func physicalPath(_ url: URL) throws -> String {
        var ancestor = url
        var suffix: [String] = []
        while !fm.fileExists(atPath: ancestor.path) {
            guard ancestor.path != "/" else { throw failure("Cannot resolve installation path.") }
            suffix.insert(ancestor.lastPathComponent, at: 0)
            ancestor.deleteLastPathComponent()
        }
        guard let resolved = realpath(ancestor.path, nil) else { throw failure("Cannot resolve installation path.") }
        defer { free(resolved) }
        var result = String(cString: resolved)
        for component in suffix { result += "/" + component }
        return result
    }

    /// Refuse aliases, nested storage, and changing the root of an existing nonempty store.
    func validatedPaths(_ values: [String]) throws -> [URL] {
        let paths = try values.map { value -> URL in
            guard value.hasPrefix("/"), !value.contains("\0") else { throw failure("Choose absolute folder paths.") }
            let url = URL(fileURLWithPath: value).standardizedFileURL
            guard url.resolvingSymlinksInPath().path == url.path, url.path != "/" else { throw failure("Choose canonical folders without symbolic links.") }
            return url
        }
        for (index, first) in paths.enumerated() {
            for second in paths.dropFirst(index + 1) {
                guard first.path != second.path, !first.path.hasPrefix(second.path + "/"), !second.path.hasPrefix(first.path + "/") else { throw failure("Application, data and backup folders must be separate.") }
            }
        }
        for storage in paths.dropFirst() {
            var current = storage
                while current.path != "/" {
                    if fm.fileExists(atPath: current.appendingPathComponent(".git").path) || current.pathExtension == "app" {
                        throw failure("Artifacts and backups must be outside source repositories and application bundles.")
                    }
                current.deleteLastPathComponent()
            }
        }
        let oldData = previous?["dataDirectory"] as? String ?? homeDirectory.appendingPathComponent("Library/Application Support/\(product.defaultDataName)").path
        if try physicalPath(URL(fileURLWithPath: oldData)) != physicalPath(paths[1]), fm.fileExists(atPath: oldData), !(try fm.contentsOfDirectory(atPath: oldData)).isEmpty {
            throw failure("Existing data was found at \(oldData). Keep that folder for this update. Data migration must be performed separately.")
        }
        return paths
    }

    /// Query actual executable paths, including headless MCP owners, before replacing/deleting a bundle.
    func hasRunningExecutable(_ destination: URL) throws -> Bool {
        var processIds = [Int32](repeating: 0, count: 65536)
        let processBytes = proc_listpids(UInt32(PROC_ALL_PIDS), 0, &processIds, Int32(processIds.count * MemoryLayout<Int32>.stride))
        guard processBytes > 0 else { throw failure("Cannot verify inactive application processes.") }
        var executableBuffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        for pid in processIds.prefix(Int(processBytes) / MemoryLayout<Int32>.stride) where pid > 0 {
            if proc_pidpath(pid, &executableBuffer, UInt32(executableBuffer.count)) > 0 {
                // Foundation and libproc use different spellings for macOS /var ↔ /private/var.
                let executable = URL(fileURLWithPath: String(cString: executableBuffer)).resolvingSymlinksInPath().standardizedFileURL.path
                let application = destination.resolvingSymlinksInPath().standardizedFileURL.path
                if executable.hasPrefix(application + "/") { return true }
            }
        }
        return false
    }

    /// Retire only this transaction's inactive old app; embedded stores/local configuration are retained.
    func retireRollback(_ path: URL, bundleId: String) {
        do {
            let attributes = try fm.attributesOfItem(atPath: path.path)
            guard attributes[.type] as? FileAttributeType == .typeDirectory,
                  path.pathExtension == "app", !runningBundles().contains(path),
                  !(try hasRunningExecutable(path)), !(try openFileOwners(path)) else { return }
            let info = try Data(contentsOf: path.appendingPathComponent("Contents/Info.plist"))
            guard let value = try PropertyListSerialization.propertyList(from: info, format: nil) as? [String: Any],
                  value["CFBundleIdentifier"] as? String == bundleId else { return }
            let protected: Set<String> = [".atlas", ".lancedb", "agent-activity", "data-schema.json", "projects.json", "appsettings.local.json"]
            var scanFailed = false
            guard let entries = fm.enumerator(at: path, includingPropertiesForKeys: [.isSymbolicLinkKey], errorHandler: { _, _ in
                scanFailed = true
                return false
            }) else { return }
            var count = 0
            for case let item as URL in entries {
                count += 1
                guard count <= 100000, !protected.contains(item.lastPathComponent.lowercased()),
                      !["lance", "sqlite", "sqlite3", "db"].contains(item.pathExtension.lowercased()) else { return }
                if try item.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true { entries.skipDescendants() }
            }
            let finalAttributes = try fm.attributesOfItem(atPath: path.path)
            let finalInfo = try Data(contentsOf: path.appendingPathComponent("Contents/Info.plist"))
            guard !scanFailed, finalInfo == info,
                  finalAttributes[.systemNumber] as? NSNumber == attributes[.systemNumber] as? NSNumber,
                  finalAttributes[.systemFileNumber] as? NSNumber == attributes[.systemFileNumber] as? NSNumber,
                  !(try hasRunningExecutable(path)), !(try openFileOwners(path)) else { return }
            try fm.removeItem(at: path)
        } catch {
            // Successful installation is not rolled back because a busy old copy needs later cleanup.
            NSLog("Previous application retained for safe cleanup: %@", path.lastPathComponent)
        }
    }

    /// Stage and rollback bundles; never copy, reset, migrate or delete an existing user store.
    func performInstall(_ values: [String], closeToTray: Bool, launchAtLogin: Bool, installWidgets: Bool = false) throws {
        let paths = try validatedPaths(values); let destination = paths[0]
        let oldConfig = try? Data(contentsOf: locator())
        guard destination.pathExtension == "app" else { throw failure("The application destination must end with .app.") }
        if fm.fileExists(atPath: destination.path), !(try fm.contentsOfDirectory(atPath: destination.path)).isEmpty {
            let infoURL = destination.appendingPathComponent("Contents/Info.plist")
            guard let infoData = try? Data(contentsOf: infoURL),
                  let info = try? PropertyListSerialization.propertyList(from: infoData, format: nil) as? [String: Any],
                  info["CFBundleIdentifier"] as? String == product.bundleId,
                  fm.fileExists(atPath: destination.appendingPathComponent("Contents/MacOS/\(product.binary)").path) else {
                throw failure("The selected application belongs to another product. Choose an empty destination.")
            }
        }
        let running = runningBundles().contains { $0.standardizedFileURL == destination }
        let headlessRunning = try hasRunningExecutable(destination)
        let resourceOwner = fm.fileExists(atPath: destination.path) ? try openFileOwners(destination) : false
        guard !running && !headlessRunning && !resourceOwner else { throw failure("Quit \(product.name) from its menu-bar icon before updating. Closing its window may keep it running.") }
        let source = resourcesDirectory.appendingPathComponent("Payload/\(product.name).app")
        guard fm.fileExists(atPath: source.appendingPathComponent("Contents/MacOS/\(product.binary)").path) else { throw failure("Installer payload is incomplete.") }
        for path in [destination.deletingLastPathComponent(), paths[1], paths[2]] {
            try fm.createDirectory(at: path, withIntermediateDirectories: true)
            guard fm.isWritableFile(atPath: path.path) else { throw failure("Folder is not writable: \(path.path)") }
        }
        let staging = destination.deletingLastPathComponent().appendingPathComponent(".\(product.appId)-\(UUID().uuidString).app")
        let rollback = destination.deletingPathExtension().appendingPathExtension("previous-\(UUID().uuidString).app")
        defer { try? fm.removeItem(at: staging) }
        try run("/usr/bin/ditto", [source.path, staging.path])
        try verifySignature(staging)
        let hadPrevious = fm.fileExists(atPath: destination.path)
        if hadPrevious {
            guard !(try hasRunningExecutable(destination)), !(try openFileOwners(destination)) else { throw failure("Application became active during staging.") }
            try fm.moveItem(at: destination, to: rollback)
        }
        do {
            try fm.moveItem(at: staging, to: destination)
            var config: [String: Any] = ["schemaVersion": "ag-desktop/install-v1", "appId": product.appId, "applicationPath": try physicalPath(destination), "dataDirectory": try physicalPath(paths[1]), "backupDirectory": try physicalPath(paths[2]), "closeToTray": closeToTray, "launchAtLogin": launchAtLogin]
            if let port = previous?["preferredPort"] {
                guard let number = port as? Int, (1024...65535).contains(number) else { throw failure("Invalid previous preferred port.") }
                config["preferredPort"] = number
            }
            let location = locator(); try fm.createDirectory(at: location.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]).write(to: location, options: .atomic)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: location.path)
            if installWidgets { try installWidgetCompanion() }
            try verifySignature(destination)
            // RunAtLoad may start the new backend; it must be the final fallible installation step.
            try configureLogin(destination, enabled: launchAtLogin)
        } catch {
            if let oldConfig { try? oldConfig.write(to: locator(), options: .atomic) }
            else { try? fm.removeItem(at: locator()) }
            try? fm.removeItem(at: destination)
            if hadPrevious { try? fm.moveItem(at: rollback, to: destination) }
            throw error
        }
        // Rollback is transaction-local. A successful verified update must not accumulate old apps.
        if hadPrevious { retireRollback(rollback, bundleId: product.bundleId) }
    }

    /// Install one shared provider without requiring any service or Ag Design to be running.
    func installWidgetCompanion() throws {
        let source = resourcesDirectory.appendingPathComponent("Widgets/AG Requests Widgets.app")
        let target = homeDirectory.appendingPathComponent("Applications/AG Requests Widgets.app")
        let identity = "io.github.ponomarevra.agdesktop.widgets"
        let targetAttributes = try? fm.attributesOfItem(atPath: target.path)
        guard targetAttributes?[.type] as? FileAttributeType != .typeSymbolicLink,
              target.resolvingSymlinksInPath().path == target.standardizedFileURL.path else {
            throw failure("The shared widget destination must not contain symbolic links.")
        }
        if fm.fileExists(atPath: target.path) {
            let info = try Data(contentsOf: target.appendingPathComponent("Contents/Info.plist"))
            guard let value = try PropertyListSerialization.propertyList(from: info, format: nil) as? [String: Any],
                  value["CFBundleIdentifier"] as? String == identity else { throw failure("A different application occupies the shared widget destination.") }
        }
        guard !runningBundles().contains(target) else { throw failure("Quit AG Requests Widgets before updating its provider.") }
        let staging = target.deletingLastPathComponent().appendingPathComponent(".ag-widgets-\(UUID().uuidString).app")
        let rollback = target.deletingLastPathComponent().appendingPathComponent("AG Requests Widgets.previous-\(UUID().uuidString).app")
        try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: staging) }
        try run("/usr/bin/ditto", [source.path, staging.path])
        try verifySignature(staging)
        let previous = fm.fileExists(atPath: target.path)
        if previous { try fm.moveItem(at: target, to: rollback) }
        do {
            try fm.moveItem(at: staging, to: target)
            try run("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", ["-f", target.path])
            let extensionURL = target.appendingPathComponent("Contents/PlugIns/AGRequests.appex")
            guard fm.fileExists(atPath: extensionURL.path) else { throw failure("Widget extension is missing.") }
            try run("/usr/bin/pluginkit", ["-a", extensionURL.path])
            try verifySignature(target)
        } catch {
            let extensionURL = target.appendingPathComponent("Contents/PlugIns/AGRequests.appex")
            try? run("/usr/bin/pluginkit", ["-r", extensionURL.path])
            try? run("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", ["-u", target.path])
            try? fm.removeItem(at: target)
            if previous {
                try? fm.moveItem(at: rollback, to: target)
                try? run("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", ["-f", target.path])
                try? run("/usr/bin/pluginkit", ["-a", extensionURL.path])
            }
            throw error
        }
        if previous { retireRollback(rollback, bundleId: identity) }
    }

    /// Register only an explicitly selected per-user login agent, without privileged helpers.
    func configureLogin(_ application: URL, enabled: Bool) throws {
        let label = "com.ponomarev.agdesktop.\(product.appId)"
        let path = homeDirectory.appendingPathComponent("Library/LaunchAgents/\(label).plist")
        let domain = "gui/\(getuid())"
        if fm.fileExists(atPath: path.path) { try? run("/bin/launchctl", ["bootout", domain, path.path]) }
        if enabled {
            let value: [String: Any] = ["Label": label, "ProgramArguments": [application.appendingPathComponent("Contents/MacOS/\(product.binary)").path], "RunAtLoad": true, "ProcessType": "Interactive"]
            try fm.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
            try PropertyListSerialization.data(fromPropertyList: value, format: .xml, options: 0).write(to: path, options: .atomic)
            try run("/bin/launchctl", ["bootstrap", domain, path.path])
        } else if fm.fileExists(atPath: path.path) { try fm.removeItem(at: path) }
    }

    /// Execute fixed system utilities with separate arguments, never shell interpolation.
    func run(_ executable: String, _ arguments: [String]) throws {
        let process = Process(); process.executableURL = URL(fileURLWithPath: executable); process.arguments = arguments
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw failure("Installation utility failed: \(URL(fileURLWithPath: executable).lastPathComponent)") }
    }

    /// Open the installed bundle after explicit user action.
    @objc func openInstalled() { NSWorkspace.shared.open(URL(fileURLWithPath: fields[0].stringValue)) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func failure(_ message: String) -> NSError { NSError(domain: "AGDesktopSetup", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    func fatalDialog(_ message: String) { let alert = NSAlert(); alert.messageText = message; alert.runModal(); NSApp.terminate(nil) }
}
#if !TEST_SETUP
@main struct SetupLauncher {
    /// Keep the application delegate alive for the complete native event loop.
    static func main() {
        let app = NSApplication.shared
        let delegate = Setup()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}
#endif
