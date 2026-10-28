# Shared local builds and captures

Use `app/Invoke-LaneResource.ps1` around a command that owns a shared output or GPU. All participating chats use the same resource names and reservation directory, independent of their checkout. Defaults are shared across worktrees for the current Windows account, not across accounts or machines. This is opt-in coordination, not a scheduler or a detector of jobs launched elsewhere.

Recommended names on the LifeShare workstation:

| Resource | Reserve for |
| --- | --- |
| `lifeshare-build` | Builds that share the canonical build/staging output |
| `gpu` | Unreal rendering, game captures, and GPU Blender renders |

Use another project/output-specific name for genuinely independent builds. An Unreal capture that also modifies the shared build must request both. Reserve the entire operation, not just its startup. Do not launch a detached background job inside the command: descendants terminate when the command returns.

```powershell
# Run from PowerShell. Adjust executable/project paths to the actual installation.
& .\app\Invoke-LaneResource.ps1 -Resource gpu -Owner 'Hearth validation' `
  -Purpose 'Import and capture frozen Desk package' -WaitSeconds 300 `
  -Executable 'E:\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe' `
  -Arguments '"E:\Review\LifeShare.uproject" -run=pythonscript -script="E:\Review\validate.py" -unattended -abslog="E:\Review\validation.log"'

& .\app\Invoke-LaneResource.ps1 -Resource gpu,lifeshare-build -Status |
  ConvertTo-Json -Depth 5
```

`-Arguments` is a Windows command line passed directly to the executable, with no shell expansion. Invoke PowerShell explicitly for scripts, or `cmd.exe` explicitly for batch files. Use the program's file logging: the job runs without a visible console and does not forward its output. The wrapper returns the direct command's exit code. Failure to acquire or launch is an error, never permission to proceed without the reservation.

Reservations use OS-held file handles. A crashed owner automatically releases its handles; stale JSON receipts never count as ownership. A Windows Job Object starts the child suspended, attaches it before execution, and terminates its descendants if the owner dies or the direct command returns. If Windows refuses job assignment, the command is terminated before it runs. No process is killed by name, and unrelated editors/runners remain untouched.

Multiple names are attempted in sorted order. If one is busy, all partial reservations are released before retrying. `-WaitSeconds 0` fails immediately; waiting is bounded and does not promise FIFO fairness. `-Status` is a snapshot; owner metadata may briefly show the previous holder during acquisition. Do not delete lock files or use timestamps to steal a reservation. The persistent receipts record owner, purpose, PID, executable, directory and time; command arguments are intentionally omitted.

Before adoption, coordinate manually with already-running jobs. Installing the tool cannot retroactively reserve an editor. A live reservation does not prove that assets, imports, or captures pass their own acceptance checks.

Validation: `powershell.exe -NoProfile -File app/TestLaneResources.ps1` exercises actual Windows processes, same-resource contention, independent-resource execution, exit codes, owner termination, child/grandchild cleanup and automatic reacquisition in an isolated directory.
