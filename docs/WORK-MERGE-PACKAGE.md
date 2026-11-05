# Feature work, merge windows and playable packages

## Operating policy

Treat writing, integration and packaging as separate queues. A PR becoming ready
adds it to the integration backlog; it should not unexpectedly interrupt a
feature-writing session. A paused or failed integration is visible work waiting
for recovery, not a reservation of every worker slot.

For the current LifeShare session, keep merging held and one normal worker
enabled. Existing repairs finish normally. Preserve round 23 and its recorded
acceptance; do not start a fresh round, reset its checkout or repeat passed work
merely to clear the queue. Verify fingerprints and current remote/LFS access
before reusing its checks or publishing.

## Controls available today

| Phase | Existing controls | Expected behavior |
| --- | --- | --- |
| Feature work | Workers above zero; merging held | Independent issues and reviews continue; ready PRs accumulate. |
| Merge window | Resume merging; Merge now for an explicitly authorized retry | An eligible round reserves capacity; running jobs finish and heavy work waits for the shared reservation. |
| Deferred/recovering merge | Hold merging or leave an interrupted round unauthorized | Preserve candidate, generated assets and receipts; unrelated workers continue. |
| Package | Project's trusted package workflow against an accepted revision | Build a named standalone artifact; report its exact source, tests and output directory. |

Workers set to zero means normal coding is paused. It does not mean merge-only
work is guaranteed to start: merge holds, dependencies, authentication, agent
availability and shared resource ownership still apply. Read the live reason.

Cleanup may delay a merge that is actually eligible to run. It must not claim
merge priority while merging is held or a saved round awaits authorization.
An already running merge keeps its reservation even if future merges are held.

## Recommended next product changes (design, not implemented here)

Make the normal mode **Feature work**, with explicit **Queue merge** and
**Queue package** actions. Keep an activity indicator independent of queue state.

1. Queue merge records a requested window, target branch and PR list preview.
   Show dependency exclusions, expected checks, last measured duration and the
   reason it cannot start. Until the window is due, feature dispatch continues.
2. At the window, stop dispatching new conflicting work, let active sessions
   finish, freeze exact PR heads and source/asset inputs, then acquire the shared
   heavy-work reservation. Changes arriving later go into the next batch.
3. Run required compile, import, gameplay and visual checks once for that frozen
   candidate. Reuse only matching receipts. Release reservations only after
   owned child processes finish. Failure preserves the candidate and opens a
   focused repair; unrelated feature work resumes when no heavy job is active.
4. Queue package references an explicit accepted revision or depends on a named
   merge request. A package queued behind a merge cannot silently use an older
   revision. Give due package requests priority over a new merge batch.
5. Publish the source revision, included changes, acceptance result, build stamp,
   artifact path and creation time. Keep the last accepted playable package
   clearly separate from current integration binaries. Package once per chosen
   release/playtest window, rather than once per feature PR.

The queue needs durable request IDs and states: queued, draining, running,
blocked, complete and cancelled. Restart must recover the same request without
duplicate jobs. Cancellation must preserve work and explain whether running
checks finish. Expose the next planned action and its waiting reason on Overview.

Before implementing this design, add scheduler regressions for future windows,
paused/failed merges, package dependency identity, restart deduplication,
conflicting heavy jobs and stale worker capacity reservations. Reuse the existing
merge, recovery, build and package mechanisms rather than introducing a second
pipeline.
