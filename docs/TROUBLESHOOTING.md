# Troubleshooting

| What you see | What to check |
| --- | --- |
| Setup screen; build buttons disabled | Enter project details and choose **Save and use**. |
| No build has reported yet | Check **Projects → Status file** and confirm your runner writes that exact path. |
| Status could not be read | Check JSON syntax and the `updated`/`started` timestamps. The app retains its last valid snapshot. |
| GitHub access or permission error | Check the selected repository, token access, organization approval, and operation permissions. |
| Retry available at a later time | GitHub returned an error/rate limit. Previous results remain visible; wait until the stated time. |
| No matching runner services | Check the service filter. A runner started manually may not be installed as a Windows service. |
| Could not pause | The pause file's parent folder must exist and be writable. The runner must observe that marker. |
| Next build fails to link or write into the workspace | A cancelled run left a process holding the files. See [Cancelled builds and leftover processes](CANCELLATION.md): list what holds the workspace, then clear it or restart the runner service. |
| This profile changed on disk | Reopen the saved profile before editing again; another process changed it. |
| Drive not found | Change the **Build drive** in advanced settings for this computer. |
| MSI reports another installation is running | Finish the other installer or restart Windows before trying again. Direct installation does not need Windows Installer. |

## The app will not start

Run the source script from **Windows PowerShell 5.1**, not PowerShell 7, to see its error:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\LaneApp.ps1
```

The normal desktop launcher starts Windows PowerShell without a console and clears inherited module paths. If you are launching tests from another PowerShell version and modules cannot be found, open Windows PowerShell directly.

If a saved project JSON was edited by hand and is invalid, correct it in `%APPDATA%\Buildlane\Projects`. To open a different known-good profile explicitly:

```powershell
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\LaneApp.ps1 -Profile C:\Profiles\project.json
```

## Reporting a bug

Use the repository's **New issue → Bug report** template. Include the steps, expected result, Windows version, and exact error. Screenshots help; hide tokens and private project details. Do not attach the credentials folder or an unreviewed profile export.
