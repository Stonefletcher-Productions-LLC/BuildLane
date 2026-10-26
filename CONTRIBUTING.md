# Contributing to BuildLane

Create a focused branch and open a pull request. Keep Windows PowerShell 5.1 compatibility; the desktop launcher uses that runtime. Preserve UTF-8 BOMs in scripts containing non-ASCII text, because Windows PowerShell otherwise reads them using the system code page.

## Local checks

Run in Windows PowerShell from the repository root:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneApp.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneGitHub.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneOnboarding.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneMoves.ps1
```

Tests use isolated temporary profiles and mocked GitHub responses. The onboarding test drives the actual Save button. The GitHub test catches Windows PowerShell array-enumeration errors across multiple pages.

Optional read-only live test, using your own configured profile and available authentication:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneGitHub.ps1 -Live -Profile C:\Profiles\project.json
```

## Screenshots

The documentation images are rendered from the real Windows Forms interface with synthetic data. Regenerate them after UI changes:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneOnboarding.ps1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneMoves.ps1 -PreviewDirectory .\docs\images
```

Review the resulting images for clipping and accidental personal data before committing them.

## Layout

| Location | Purpose |
| --- | --- |
| `app/LaneApp.ps1` | Main dashboard and GitHub mutations |
| `app/LaneProjectsUi.ps1` | Project editor and first-run setup |
| `app/LaneMoves.ps1` | Background bulk moves, progress, and partial results |
| `app/LaneProfiles.ps1` | Profile validation and persistence |
| `app/LaneRefresh.ps1`, `app/LaneGitHub.ps1` | Background reads and pagination |
| `app/BuildLaneLauncher.cs` | Console-free Windows launcher |
| `app/InstallLaneApp.ps1`, `app/BuildLaneInstaller.ps1` | Local installation and MSI packaging |
| `docs/`, `examples/` | User guides and sample integrations |

Update the README and relevant guide when behavior changes. Do not add credentials, personal profiles, generated binaries, or game assets. Signing and production release validation are separate from regression tests.
