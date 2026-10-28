# Cancelled builds and leftover processes

A cancelled step does not cancel what it started. GitHub ends the step's shell process, and Windows ends nothing else: an editor, commandlet, compiler server or capture that the step launched keeps running after the run disappears from the Actions page. It holds the workspace's output files open, so every later job on that runner fails to write them - typically `LNK1104: cannot open file '<dll>'; it is being used by another process` - while the build code is fine. On a shared PC the same thing happens without a cancellation, when a chat or another runner already has the project open.

BuildLane cancels runs for you: **Pause** stops the running build (`POST /actions/runs/{id}/cancel`), and a queue or release action stops a batch that is no longer wanted. So a backend that BuildLane drives must be able to clear its own leftovers, and BuildLane does not do it for you. [Resource reservations](RESERVATIONS.md) cover jobs launched through `Invoke-LaneResource.ps1`; processes a runner service starts are outside that mechanism.

## What a backend has to do

1. **On cancellation, end the job's process tree.** Register a step that runs only when the job is cancelled and stops everything the job started, scoped to the job's workspace. A cancelled job's cleanup must not turn the run red: it is already cancelled, so give the step `continue-on-error: true` and let its output say what survived.
2. **Before starting work, find out whether the workspace is already in use.** Wait for a developer's editor to close rather than colliding with it, and clear a leftover commandlet from an earlier run. Waiting with a printed reason every few minutes beats a job that fails after twenty.

Both halves need the same two checks, because neither alone is sufficient:

| Check | Finds | Misses |
| --- | --- | --- |
| Command line names the workspace | A process the current account can inspect | A process in another session or running as another account; its command line reads as empty |
| A workspace file cannot be opened for writing | Any holder at all, including one that cannot be inspected | Which process it is, and whether it is yours |

Build first on the file probe: it is the decisive test, and it is the one that still works when the culprit is a service-owned process in session 0 that `Stop-Process` cannot even see. Use the command line only to decide *which* processes to stop.

## Reference implementation

[`examples/ci/Stop-JobProcesses.ps1`](../examples/ci/Stop-JobProcesses.ps1) is a small, dependency-free script to copy into a backend's CI folder. It matches processes by workspace path or by a probe path that cannot be opened for writing, always excludes itself and its ancestors, and exits 1 when the workspace is still busy. No process is matched by name alone.

Preflight - wait while someone else's editor or a leftover commandlet has the workspace:

```powershell
# Wait while a developer's editor or an earlier job still has the workspace.
$Deadline = (Get-Date).AddHours(8)
while ((Get-Date) -lt $Deadline) {
    if (& .\ci\Stop-JobProcesses.ps1 -Workspace $PWD -ProbePath "$PWD\Binaries" -List) { exit 0 }
    Write-Host "WAITING: the workspace is still in use. Retrying in 5 minutes."
    Start-Sleep -Seconds 300
}
throw "The workspace stayed busy for 8 hours: $PWD"
```

Clear what a cancelled job left, as the last step of the job:

```yaml
- name: Stop what this job left running
  if: cancelled()
  continue-on-error: true
  shell: powershell
  run: ./ci/Stop-JobProcesses.ps1 -Workspace $PWD -ProbePath "$PWD\Binaries" -MaxWaitSeconds 30
```

## Limits

A process that a Windows service started in another session cannot be stopped from an unelevated step, and the script says so instead of pretending to have cleared it. Recovery there is stopping that runner service, which ends its whole session, or running the cleanup elevated. The probe keeps reporting the lock either way, so the next job's Preflight fails with a reason instead of a linker error.

Validation: `powershell.exe -NoProfile -File app/TestLaneProcesses.ps1` starts a job that leaks a grandchild and holds a file open, then checks that listing reports it, cleanup ends both processes and releases the file, a similarly named workspace is left alone, and a lock no process can be attributed to is still reported.
