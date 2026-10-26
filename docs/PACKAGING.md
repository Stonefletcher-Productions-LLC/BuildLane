# Packaging and signing

## Direct installation

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\InstallLaneApp.ps1
```

This builds the GUI launcher with the local .NET Framework compiler and copies the app to `%LOCALAPPDATA%\Programs\Buildlane`. It creates a desktop shortcut. Saved project profiles remain in `%APPDATA%\Buildlane\Projects`.

For an isolated build without changing desktop shortcuts:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\InstallLaneApp.ps1 -Destination .\build\app -NoShortcut
```

## MSI candidate

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\BuildLaneInstaller.ps1 -Version 1.2.0
```

The builder uses Windows Installer COM, `makecab.exe`, and the .NET Framework compiler. No additional installer SDK is required. It produces a per-user MSI with desktop and Start menu shortcuts under `%LOCALAPPDATA%\Buildlane-Packages`. Use `-OutputDirectory` to choose another build folder.

Unsigned files include `-unsigned` in the MSI name. Keep generated packages out of Git; publish a release only after validation and explicit release approval.

## Signing when credentials are available

Install the organization's valid code-signing certificate and private key in the current user's certificate store, then supply its thumbprint:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\app\BuildLaneInstaller.ps1 -Version 1.2.0 -CertificateThumbprint YOUR_CERTIFICATE_THUMBPRINT
```

The builder signs the executable, scripts, and MSI using SHA-256 and verifies timestamped signatures. `-TimestampServer` overrides the timestamp service. Never commit a private key, password, or PFX file.

## Release readiness

Signing credentials are still pending. Package creation passed for the previous candidate, but the final MSI install/upgrade/uninstall sweep was blocked by a stuck Windows Installer transaction. Before distribution, validate a fresh install, upgrade with saved profiles preserved, launch from both shortcuts, downgrade handling, uninstall, and signature verification on a clean Windows machine. Building an MSI alone does not establish that those checks passed.
