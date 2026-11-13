# Desktop releases

Read [the changelog](../CHANGELOG.md) for user-facing release notes. Download
installers from [GitHub Releases](https://github.com/PonomarevRA/ag-tasks/releases).
The available builds are macOS arm64, Windows x64, and Linux x64. The macOS
community installer is ad-hoc signed and requires the usual local approval for
an unnotarized app. Check the downloaded installer against `SHA256SUMS`.

The installed app checks the public release feed in the background. The Help
menu offers a manual check and a link to the official releases page. An agent
can include an update suggestion in its final response after finishing work.
If the same release is still available, the suggestion can appear again after
three days. Choose when to open the release page and run the installer.

To prepare a release, update all four version declarations (Python package,
runtime, native bundle and installer manifest) and add a `## [X.Y.Z]` entry to
`CHANGELOG.md`. Write 1–5 plain-text bullets per language under `### Русский`
and `### English`, in that order. Each bullet is at most 160 characters.
Describe an improvement users can see, such as a completed workflow, clearer
feedback or fewer interruptions. Keep internal tickets, paths, private data,
links and implementation details out of the notes.

Run `python packaging/release.py --tag vX.Y.Z` with the exact release version
in the reviewed public checkout. Push the reviewed commit to public `main`
and tag that commit with `vX.Y.Z`. GitHub Actions builds and tests each
platform independently, verifies the installers and their checksums, and
publishes them together with notes drawn from that version's changelog entry.
A failed build or missing installer prevents publication. Manually dispatch
the workflow on a branch to build and test without creating a release.
