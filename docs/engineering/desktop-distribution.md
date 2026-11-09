# Desktop distribution and local request snapshots

The standalone `ag-tasks` application vendors `desktop_support` Python sources; no sibling checkout is needed at runtime. `packaging/desktop-product.json` pins the product and official repository. Release publication is separate from building a local installer. macOS builds are ad-hoc signed, without notarization or a trusted-signing claim.

## Storage and process ownership

`AGDesktop/ag-tasks/installation.json` is resolved through the platform configuration root documented by the shared installation schema. Explicit `--data-dir` and the existing service environment override take precedence; otherwise the validated installation locator selects the data directory. With no locator the previous service default remains. Invalid configuration fails explicitly. The backup directory is displayed; this integration does not create or schedule backups or migrate a store. Startup logging uses the same selected data directory.

The preferred locator port is used only when `--port` is absent. Native startup reserves its own loopback socket; a busy port falls back to an owned ephemeral port and never attaches to an unrelated server. Restoring a hidden window reuses that window and the existing backend.

The close button hides only after the native tray actually reports ready and close-to-tray is enabled. Unsupported or failed tray setup preserves normal shutdown. Tray Quit, Cmd-Q, signals and OS session end always use the existing bounded drain/watchdog. Launch-at-login remains an explicit installer choice. No global toolchain or license state is changed.

## Read-only desktop snapshot

`GET /api/desktop/v1/widget` uses the existing loopback/Host/Origin boundary and `Cache-Control: no-store`. `schemaVersion=ag-desktop/widget-v1` and `appId=ag-tasks` identify the response. `capturedAt` is UTC; the newest eight rows have bounded plain strings, actual operation status and optional duration. Unknown identity and missing summaries remain explicit. No source bodies, credentials, task contents or arbitrary file reads are exposed. Widget polling is excluded from request capture. A local HTML request view is not a registered native OS widget.

Tasks has no durable activity journal. It retains32 completed matched API-route records in process memory and returns only the newest eight. Request bodies, query values and concrete task IDs are never captured; the route template describes the actual HTTP operation. Agent-route use is labelled as an unidentified client, not authenticated agent attribution. Restarting intentionally clears this limited history. Tray Agent requests opens `/desktop-activity.html` in the existing window; it polls every2s while visible, aborts after3s, labels stale failures and keeps all text inert.


The optional WidgetKit companion is distributed separately. The standalone app includes its own recent-request view.
