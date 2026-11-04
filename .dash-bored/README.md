# This project's dash-bored dashboard

[dash-bored](https://github.com/ArvidSU/dash-bored) is a local desktop app that
turns a project's commands, checks, docs, and services into a shared dashboard.
This directory is its portable dashboard bundle; it travels with the project.

- `dash-bored.yaml`: dashboard layout, panels, actions, and schema version.
- `dash-bored-lock.yaml`: exact revisions of external components and themes.
- `components/`: project-local and pinned external component code.
- `.env`: dashboard command variables and an optional ACP agent command
  (`DASH_BORED_AGENT`); keep credentials out of version control.
- `install-app.sh`: downloads, verifies, and opens a compatible macOS installer.

## Managed packages

The lock includes the release-pinned core package. Dash-bored restores missing
checkouts at exact pins on opening, including linked bundles. Git is required;
no dependency installation, parent Git repository or Git identity is needed.
Private repositories in `components/external/` and `themes/external/` belong
to dash-bored. This bundle's generated ignore rules exclude those directories;
commit the YAML, local components and lock file. Existing checkouts are untouched
until explicit Sync or Update. Inspection and file reloads do not download.
Offline failures preserve configuration and pins and offer Retry/Sync in the app.
Older parent-owned submodules require explicit ownership migration, which refuses
staged registration changes or package edits.

## Install and open

Requires an Apple Silicon Mac with macOS 14 or newer. No Bun, Node, or coding
agent is needed to view the dashboard. Download manually from
[GitHub Releases](https://github.com/ArvidSU/dash-bored/releases), or run this
from the project root:

```sh
sh '.dash-bored/install-app.sh'
```

The command reads this bundle's `schemaVersion`, selects the newest published
canary release with that exact dashboard contract (no migration), checks the
DMG's SHA-256, and opens it. It stops if no compatible release is published.

1. Drag **dash-bored-canary** from the installer into **Applications** and open it.
   If macOS blocks this unsigned prerelease, try opening it once, then choose
   **System Settings → Privacy & Security → Open Anyway**.
2. Choose **Add dashboard** and select this project's `.dash-bored` directory.
   The app loads this bundle without replacing its configuration.
3. Review the requested capabilities before choosing **Trust project** to enable
   checks, commands, and project-file access. Manifests can be inspected before trust; component execution follows trust.
4. For a starter dashboard, follow **Get started**: choose a coding agent that
   speaks the Agent Client Protocol (the app detects known ones on your PATH),
   install the dash-bored skill globally or for this project, then use **Set up
   this dashboard** and review the agent and prompt before **Send**.
   Existing dashboards are ready to use; use the component library or your agent
   to customize them. Save dashboard edits with **Save dashboard**.

Press **Command-K** to find app, dashboard, and component actions. App updates
and schema migrations are available separately in the app.
