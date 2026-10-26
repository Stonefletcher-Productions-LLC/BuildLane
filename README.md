# BuildLane

**Your build queue, releases, and runner health in one Windows desktop app.**

Built by Stonefletcher Productions LLC. Choose a project, see what is building, and manage releases without keeping a terminal open.

![BuildLane overview with sample build data](docs/images/overview.png)

[Get started](docs/GETTING-STARTED.md) · [Connect your build system](docs/INTEGRATION.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Contribute](CONTRIBUTING.md)

## What you can do

- **See progress:** current step, elapsed time, queue, and recent logs.
- **Manage releases:** view milestones and pull requests, move PRs, and queue a compatible release workflow.
- **Switch projects:** create, import, edit, and export profiles in the app.
- **Stay responsive:** GitHub reads happen in the background, with pagination and rate-limit backoff.
- **Start simply:** first-run setup, repository URL pasting, path pickers, and expandable advanced settings.
- **Keep settings:** profiles and encrypted tokens live outside the installation folder.

BuildLane is a desktop client for an existing build system. It does not create a runner or supply a universal CI pipeline. Local status and pause controls need the [backend contract](docs/INTEGRATION.md); GitHub connectivity alone does not make an arbitrary workflow compatible.

## Start here

**Requirements:** Windows, Windows PowerShell 5.1, and the .NET Framework compiler. GitHub CLI is optional; the app also accepts a GitHub token.

1. Clone this repository, or use **Code → Download ZIP** and extract it. Organization access is required while this repository is private.
2. Open **Windows PowerShell** in the extracted repository folder.
3. Run:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\InstallLaneApp.ps1
   ```

4. Open **Buildlane** from the new desktop icon. The PowerShell window can be closed.
5. Enter your project name, paste its GitHub repository URL, and select your runner paths. Choose **Save and use**, then **Connect GitHub**.

![First-run project setup; paths shown are examples](docs/images/setup.png)

The app installs to `%LOCALAPPDATA%\Programs\Buildlane`. Existing saved projects remain selected after an upgrade. New installations start with a neutral setup screen; the optional [LifeShare example](examples/LifeShare.profile.json) is available to import.

See the [illustrated setup guide](docs/GETTING-STARTED.md) for field explanations, switching projects, and sharing profiles.

## Development and checks

Run from the repository root:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneApp.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneGitHub.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneOnboarding.ps1
```

These tests use temporary profiles and mocked GitHub responses. GitHub Actions runs the same checks on Windows. See [CONTRIBUTING.md](CONTRIBUTING.md) for optional live checks and screenshot generation.

## Installer status

The direct installation command above is the verified setup route. MSI packaging is available for testing:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\BuildLaneInstaller.ps1 -Version 1.2.0
```

Output: `%LOCALAPPDATA%\Buildlane-Packages\Buildlane-1.2.0-unsigned.msi`.

**Signing credentials are pending.** The latest MSI still needs a clean install/upgrade/uninstall validation pass; a stuck Windows Installer transaction blocked the previous development-machine sweep. Do not treat it as a signed production release. See [packaging instructions](docs/PACKAGING.md).

## Current boundaries

- Windows desktop only; the backend still needs an adapter for each build system.
- GitHub writes and token validation can briefly block the interface.
- Hosted GitHub is supported; GitHub Enterprise Server endpoints are not configurable yet.
- Screenshots show the real app with sample project data and illustrative paths.

Source originally extracted from LifeShare commit `55130dbe42ed7b77b996bec051a2f7852b9b91d0`. This repository contains the standalone app, tests, and packaging source.
