import Foundation

/// Run the same installation transaction against a fake bundle and isolated home.
@main struct SetupTests {
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("ag-setup-test-\(UUID().uuidString)").resolvingSymlinksInPath()
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let setup = Setup()
        setup.homeDirectory = root
        setup.resourcesDirectory = root.appendingPathComponent("Resources")
        setup.runningBundles = { [] }
        let payload = setup.resourcesDirectory.appendingPathComponent("Payload/Test App.app/Contents/MacOS/TestBinary")
        try fm.createDirectory(at: payload.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("version-one".utf8).write(to: payload)
        setup.product = Product(appId: "code-atlas", name: "Test App", version: "1.0.0", binary: "TestBinary", bundleId: "test.ag.setup", repository: "PonomarevRA/code-atlas", defaultDataName: "TestData")
        let info: [String: Any] = ["CFBundleIdentifier": "test.ag.setup"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: payload.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Info.plist"))
        do {
            try setup.validatePreviousInstallation(["applicationPath": "relative"])
            fatalError("Incomplete locator accepted")
        } catch {}
        try setup.loadPreviousInstallation()
        let locatorURL = setup.locator()
        try fm.createDirectory(at: locatorURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.createSymbolicLink(at: locatorURL, withDestinationURL: root.appendingPathComponent("missing-settings"))
        do { try setup.loadPreviousInstallation(); fatalError("Dangling locator accepted") } catch {}
        try fm.removeItem(at: locatorURL)
        let app = root.appendingPathComponent("Applications/Test App.app")
        let data = root.appendingPathComponent("data")
        let backup = root.appendingPathComponent("backup")
        let values = [app.path, data.path, backup.path]
        try setup.performInstall(values, closeToTray: true, launchAtLogin: false)
        precondition(setup.existingApplicationPath().path == app.path)
        try Data("accepted-record".utf8).write(to: data.appendingPathComponent("preserve.txt"))
        setup.previous = try JSONSerialization.jsonObject(with: Data(contentsOf: setup.locator())) as? [String: Any]
        setup.previous?["preferredPort"] = 12345
        try Data("version-two".utf8).write(to: payload)
        try setup.performInstall(values, closeToTray: false, launchAtLogin: false)
        let saved = try String(contentsOf: data.appendingPathComponent("preserve.txt"), encoding: .utf8)
        precondition(saved == "accepted-record")
        let executable = try String(contentsOf: app.appendingPathComponent("Contents/MacOS/TestBinary"), encoding: .utf8)
        precondition(executable == "version-two")
        let config = try JSONSerialization.jsonObject(with: Data(contentsOf: setup.locator())) as! [String: Any]
        precondition(config["closeToTray"] as? Bool == false)
        precondition(config["preferredPort"] as? Int == 12345)
        let physicalData = try setup.physicalPath(data)
        precondition(config["dataDirectory"] as? String == physicalData)
        if let exported = ProcessInfo.processInfo.environment["AG_SETUP_TEST_EXPORT"] {
            try JSONSerialization.data(withJSONObject: config, options: [.sortedKeys]).write(to: URL(fileURLWithPath: exported))
        }
        let attributes = try fm.attributesOfItem(atPath: setup.locator().path)
        precondition((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        do {
            _ = try setup.validatedPaths([app.path, data.appendingPathComponent("nested").path, data.path])
            fatalError("Nested folders accepted")
        } catch {}
        do {
            _ = try setup.validatedPaths([app.path, root.appendingPathComponent("new-data").path, backup.path])
            fatalError("Nonempty data migration accepted")
        } catch {}
        // A widget failure cannot start a login backend before rolling the bundle back.
        do {
            try setup.performInstall(values, closeToTray: true, launchAtLogin: true, installWidgets: true)
            fatalError("Missing widget source accepted")
        } catch {}
        precondition(!fm.fileExists(atPath: root.appendingPathComponent("Library/LaunchAgents/com.ponomarev.agdesktop.code-atlas.plist").path))
        precondition(fm.fileExists(atPath: data.appendingPathComponent("preserve.txt").path))
        let foreign = root.appendingPathComponent("Foreign.app")
        try fm.createDirectory(at: foreign, withIntermediateDirectories: true)
        try Data("keep-foreign".utf8).write(to: foreign.appendingPathComponent("important.txt"))
        do {
            try setup.performInstall([foreign.path, data.path, backup.path], closeToTray: true, launchAtLogin: false)
            fatalError("Foreign application accepted")
        } catch {}
        precondition(fm.fileExists(atPath: foreign.appendingPathComponent("important.txt").path))
        let installedBinary = app.appendingPathComponent("Contents/MacOS/TestBinary")
        try fm.removeItem(at: installedBinary)
        try fm.copyItem(at: URL(fileURLWithPath: "/bin/sleep"), to: installedBinary)
        let child = Process(); child.executableURL = installedBinary; child.arguments = ["5"]
        try child.run()
        // Wait for the owned child to finish exec before testing the installer's process-path guard.
        usleep(100_000)
        do {
            try setup.performInstall(values, closeToTray: true, launchAtLogin: false)
            fatalError("Headless process accepted")
        } catch {}
        child.terminate(); child.waitUntilExit()
        setup.runningBundles = { [app] }
        do {
            try setup.performInstall(values, closeToTray: true, launchAtLogin: false)
            fatalError("Running application accepted")
        } catch {}
        print("PASS native setup fresh install, update, data preservation, locator permissions, nested/migration/running-owner refusal")
    }
}
