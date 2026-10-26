# BuildLane

Windows desktop control panel for build queues, GitHub releases, and local runner health, maintained by Stonefletcher Productions LLC.

BuildLane opens as a desktop app with no command window. It includes an Overview dashboard, Releases, System health, and an in-app Projects editor. GitHub reads refresh in the background with pagination, rate-limit handling, and protection against results from a previously selected project.

## Run it

Requires Windows, Windows PowerShell 5.1, and the .NET Framework compiler installed with Windows. Run the following from this repository in Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\InstallLaneApp.ps1
```

Open **Buildlane** from the desktop shortcut. The installer script builds the launcher and installs the app in `%LOCALAPPDATA%\Programs\Buildlane`. It does not install or reconfigure runner services.

The bundled profile is the LifeShare example. In **Projects**, create or import a profile for another project, edit its GitHub repository, branch, workflow, local status/pause paths, runner path, service filter, cache port, and disk thresholds, then select **Save and use**. Profiles and the selected project are stored in `%APPDATA%\Buildlane\Projects`.

GitHub access uses the app's token setup or existing GitHub CLI authentication. Saved app tokens are encrypted for the current Windows user. Keep credentials and personal project profiles out of this repository.

## Connecting other products

The desktop client is configurable across projects. Each project still needs a compatible build backend; this repository does not install GitHub runners, build a game, or supply a universal CI workflow.

The current backend contract comes from [LifeShare's build lane](https://github.com/Stonefletcher-Productions-LLC/LifeShare/blob/main/Docs/BuildLane.md):

- The selected workflow accepts a `workflow_dispatch` input named `task`, including `lane` for starting the queue.
- GitHub milestones represent releases, with a `lane: <state>` line in their description.
- Pull requests use the lane's `ready` and `fix-main` labels.
- The runner publishes compatible local status JSON and observes the configured pause file.
- System health uses the configured Windows services, drive, and cache endpoint.

Use the LifeShare integration as the reference when adapting another backend. Switching the profile alone does not make an unrelated workflow compatible. GitHub writes and token validation currently run synchronously.

## Validate changes

Run under Windows PowerShell 5.1, which is also the launcher's runtime:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneApp.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneGitHub.ps1
```

The tests cover status rendering, progress, profile validation and conflict handling, background refresh coordination, stale results, and multi-page GitHub array parsing. An optional `-Live` flag on `TestLaneGitHub.ps1` checks the bundled profile against GitHub using available authentication.

## Prepare an installer

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\BuildLaneInstaller.ps1 -Version 1.1.1
```

Outputs go to `%LOCALAPPDATA%\Buildlane-Packages`. The MSI is per-user and includes desktop and Start menu shortcuts. Packaging uses Windows Installer, makecab, and the .NET Framework compiler; no external installer SDK is required.

Signing credentials are pending. Without them, the output is explicitly named `Buildlane-<version>-unsigned.msi`. When a valid code-signing certificate with its private key is available in the current user's certificate store, pass `-CertificateThumbprint` and optionally `-TimestampServer` to sign the payload and MSI. Do not commit private keys or certificates containing them.

**Installer validation status:** package creation has passed. Earlier candidates passed install/uninstall and upgrade checks; the latest MSI still needs a clean install/upgrade/uninstall validation pass after resetting a stuck Windows Installer transaction on the development PC. Use the direct installation script above for the currently verified installation path.

## Source

`app/` contains the client, launcher, profile example, packaging scripts, and regression tests. BuildLane was extracted from LifeShare commit `55130dbe42ed7b77b996bec051a2f7852b9b91d0`, including the Windows PowerShell 5.1 GitHub array/date fix. This repository contains the application source, without game assets, local credentials, or generated installers.
