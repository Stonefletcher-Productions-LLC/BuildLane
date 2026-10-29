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

The documentation images are rendered from the real Windows Forms interface with synthetic data. No network calls, no
saved projects: each script builds a temporary profile and throws it away.

```powershell
# The dashboard (light and dark) and the Releases tab
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\PreviewDocs.ps1 -OutDirectory .\docs\images
# The first-run screen and the project editor
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneOnboarding.ps1 -PreviewDirectory .\docs\images
```

`PreviewDocs.ps1` writes `overview.png`, `overview-dark.png` and `releases.png`; the onboarding test writes `setup.png`
and `projects.png`. Both render at the app's normal window size, so a layout change that clips a button shows up in the
images. Review the results for clipping and accidental personal data before committing them.

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
| `app/PreviewDocs.ps1` | Renders the documentation screenshots from the real interface with fixture data |
| `app/BuildLogo.ps1` | Draws the logo and writes `app/BuildLane.ico` plus the README and social-preview images |
| `docs/`, `examples/` | User guides and sample integrations |

## Logo

The mark is a "B" for BuildLane over a lane stripe that runs from cyan to green, on a deep navy tile. It deliberately
shares no shape or colour with the LifeShare icon (a coral house with a teal gear): one product built the other, but
they are separate things.

`app/BuildLogo.ps1` draws every size with the same geometry and writes all of the places the icon lives:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\BuildLogo.ps1
```

| Output | Used by |
| --- | --- |
| `app/BuildLane.ico` (16–256 px) | Window and taskbar icon, desktop shortcut, compiled launcher |
| `docs/images/logo.png` (512 px) | The top of this README |
| `docs/images/social-preview.png` (1280×640) | GitHub's social preview (Settings → Social preview → Upload) |

The launcher embeds the icon at compile time, so `InstallLaneApp.ps1` recompiles it when either
`BuildLaneLauncher.cs` or `BuildLane.ico` changes.

Update the README and relevant guide when behavior changes. Do not add credentials, personal profiles, generated binaries, or game assets. Signing and production release validation are separate from regression tests.
