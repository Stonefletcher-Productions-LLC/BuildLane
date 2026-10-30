# Documentation screenshots

All screenshots in these guides show the real Windows Forms app with synthetic data. They are illustrations, not live build status or proof of a released package.

## Staging controls

`integration-mode.png` and `staging-release.png` were downloaded unchanged from the `documentation-fixtures` artifact of [successful Windows checks run 40988967924](https://github.com/Stonefletcher-Productions-LLC/BuildLane/actions/runs/40988967924), source commit `49ee180`. Both were visually reviewed after download. They run the app from main `214c88c` plus documentation/capture support; no production UI behavior was changed.

The sample version `0.3.0`, repeated-letter source hashes, build `123 / 2`, and green package result are mocked fixture values. **They do not certify LifeShare 0.3.0.** The blue acknowledgement sentence retains its earlier `dispatching` text while the current table row shows the later fixture result; consult the current row and selected request's exact build for status. Details below the visible portion of the release screen can be reached by scrolling.

The hosted workflow retains these screenshots for 14 days. Regenerate on a Windows test machine with:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneIntegration.ps1 -OutDirectory .\docs\images
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\TestLaneReleaseRequests.ps1 -OutDirectory .\docs\images
```

These tests open temporary fixture windows. Use hosted CI when a shared PC is reserved for gameplay or release validation. They do not dispatch real controller requests or engine jobs.

## Other images

Overview, dark Overview and milestone Releases use `app/PreviewDocs.ps1`; Setup and Projects use `app/TestLaneOnboarding.ps1 -PreviewDirectory`. These existing images illustrate the corresponding flows and predate the staging screenshots. Logo assets come from `app/BuildLogo.ps1`. See [contributing](../../CONTRIBUTING.md).
