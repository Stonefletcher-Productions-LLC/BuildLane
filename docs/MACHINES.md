# Machines: test nodes that take jobs from Fletchery

Fletchery on the PC is the one job queue and history for every build machine. A test node (SF-NODE-01 first) runs
a small worker, `fletchery/node_worker.py`, that connects **out** to Fletchery over Tailscale, asks for work, runs it
and reports back. Building → **Machines** shows the PC and each node: online or offline, the job it is running, its
last result, CPU, free RAM and disk. **Jobs** below it lists the queue and history with each job's log.

## How it is kept safe

- **Private network only.** The worker API listens on the coordinator's Tailscale address and port
  (`workers.machines.listen`, e.g. `100.83.43.111:8771`). Fletchery refuses `0.0.0.0`, `::` and public addresses.
  The tailnet ACL lets only `tag:node` reach `tag:coordinator:8771`. The dashboard stays on `127.0.0.1:8770`.
- **One key per node, revocable.** Adding a node prints its key once; Fletchery keeps only its SHA-256 hash.
  Revoking (dashboard or `machines.py revoke`) stops the key at once and queues the node's running job again.
  Ten bad sign-ins from one address in a minute are refused for a minute.
- **Jobs are recipes, not commands.** A job names one of the node's recipes (`node-checks`, `nightly-full`,
  `checkout-only`, `package`) with checked parameters (`only` is a comma list of check names; `strict_slots` is true or false)
  and a full commit SHA. The node has its own copy of the recipes and its own allow-list (`recipes` in its config),
  so a compromised coordinator still cannot make a node run anything else.
- **Read-only code access.** The node fetches the exact commit with its own read-only deploy key. It holds no GitHub
  token: Fletchery on the PC posts the commit statuses.

## Jobs and GitHub

| Recipe | What the node runs (same steps as the Buildlane workflow) | Commit status |
|---|---|---|
| `node-checks` | Stop-JobProcesses, ResetContentWorkspace, `NodeTests.ps1 -Engine -ResultJson [-Only] [-StrictSlots]`, then always Stop-JobProcesses, `git checkout -- Content/Sim`, ResetContentWorkspace | `Node checks` |
| `nightly-full` | the same with `-Set Full` | `Nightly full tests (node)` (what `ci.yml`'s nightly waits for) |
| `checkout-only` | fetch and check out the commit, print it (a connection test) | none |
| `pr-check` | the PC check of one worker PR: check out the base branch tip (the job's commit), fetch the PR head, Stop-JobProcesses, ResetContentWorkspace, merge the head (`--no-ff`), `Tools/CI/PrCheck.ps1 -BaseRef <base>`; then always Stop-JobProcesses, `git merge --abort`, ResetContentWorkspace | none: Fletchery labels the PR `ready` or posts the failure (see PR checks) |
| `package` | a build scheduled on Building → Builds: Stop-JobProcesses, ResetContentWorkspace, `Build.ps1` and `BuildContent.ps1` (or `NodeTests.ps1` Full when "Full tests first" is ticked), `Package.ps1 -Version <v> -Out <buildsDir>\LifeShare-<v>`, then the same clean-up | none |

A job needs the labels `os:windows` and `role:tests` and 16 GB of RAM (`checkout-only` needs nothing); a job can ask
for more labels, such as `unreal:5.8.4`. A node runs one job at a time, oldest first. A job whose node stops
answering for 3 minutes is queued again once, then marked lost. Logs are kept in `<state>\job-logs` (50 MB per job).

**Shadow.** Until cutover, `"shadow": true` (the default) sends the statuses as `shadow/Node checks` and
`shadow/Nightly full tests (node)`, so nothing that `ci.yml` or `lane.yml` reads changes. Set `"shadow": false` at
cutover, after the Buildlane node runner is stopped, so the two never both report.

**PR checks** (`nodecheck.py`). With `"prCheck": {"labels": ["unreal:5.8.3"]}` in `workers.machines` (and
`workers.prCheckCommand` set), Fletchery queues one `pr-check` at a time for a node that is online, not paused and has
those labels plus `os:windows` and `role:tests`. The extra label keeps PR checks off a node whose engine differs from
the PC's: a node on 5.8.4 would save content the PC's 5.8.3 can't load. Fletchery reads the finished job: a pass labels
the PR for merging, a merge conflict or a failing check is posted on the PR (the step's log tail) for its fixer, and
anything else (exit 2 from PrCheck.ps1, a fetch or worker error, a lost job) records nothing, so it runs again.
In shadow mode the node checks the same PRs as the PC and only its result is kept (`node_pr_checks` in state.json,
and a log line comparing it with the PC's); the PC still decides. With shadow off the node's result counts and the PC
stops starting its own PR checks while a node is eligible, so its idle time goes to merge rounds.

**Nightly.** With `"nightly": {"at": "07:00", "branch": "main"}` (UTC) Fletchery queues `nightly-full` on main's
head once a day, within three hours of that time. Leave it out until cutover; the Buildlane node runner still runs
the nightly until then.

## Builds (Building → Builds)

Pick a version (`0.3.9`), a branch (the base branch if left empty), a time (now, at a time, or every day at a time),
whether to run the Full tests first, and where: **Automatic**, **A test node** or **This PC**. At its time Fletchery
reads the branch once and builds that commit, whatever lands after. The build then waits for a machine:

- **Automatic** queues a `package` job when a node that can package is online: labels `os:windows` and `role:cook`
  plus any in `workers.builds.nodeLabels` (use `["unreal:5.8.3"]` until the node's engine matches the PC), at least
  16 GB of RAM, and not paused. Otherwise it builds on this PC.
- **This PC** uses the merge checkout, so a running merge round or PC check finishes first; then the build goes
  before the next round. It waits for the heavy lease like a round, and resets generated Content afterwards so the
  next round starts clean. Packages go to `workers.builds.outDir` (default `<state dir>\builds`); the log is in
  `<state dir>\build-logs`.
- A node that has not added `package` to its config's `recipes` refuses the job; an Automatic build then moves to
  this PC. The node writes packages to `buildsDir` in its config (default `<workRoot>\builds`).

A folder that already exists is never overwritten: the commit is added to the name. A daily build starts a one-off
copy each day; **Stop repeating** ends it. Builds are kept in state.json, so a restart keeps them; a PC build that was
running when Fletchery stopped is marked failed.

Profile (optional, `workers` block): `"builds": {"outDir": "E:\\lifeshare-scratch\\LifeShare-builds",
"engine": "E:\\UE_5.8", "timeoutMinutes": 300, "nodeLabels": ["unreal:5.8.3"]}`.

## A merge round's tests on a node

With `"roundTests": {"labels": ["unreal:5.8.4"]}` inside `workers.machines`, each merge round still builds and imports
content on this PC, but runs its long test suites on a test node: the merger pushes the candidate to a throwaway
`fletchery/round-<n>` branch and runs

    python machines.py --state-dir <state> run --recipe node-checks --sha <sha> --only <tests> --requested-by "round <n>"

which queues a `node-checks` job, waits, prints the result and the failing output, and exits 0 passed, 1 failed, 2 no
result. On 2 (no node free, node paused from Tests, lost, out of memory) the merger runs the tests on this PC as before.
The labels pick a node whose engine matches this PC. The job shows on the Merging card as the round's.

### Whole rounds on a node

Add `"whole": true` to `roundTests` and a round leaves this PC's Unreal alone: it doesn't wait for or take the PC heavy
lease, and the merger only does git here (merging, conflict fixes, pushes). The node builds the editor, runs
BuildContent and the checks the plan names (strict navigation too) in one `node-checks` job, and the merger lands only
after a node pass on that exact commit. A batch with a `Lane-Imports:` PR, or a round when no node can take
`node-checks` (paused, offline, shadow), runs the old way on this PC. The merger agent itself still runs on this PC; it is
a chat session and git, not Unreal.

### Import rounds on a node

A `Lane-Imports:` batch can run on a node too when `roundTests` has `"imports": true` and a node can push content. That
node needs a second deploy key with write access (`gitPushKey` in its config; the read-only `gitSshKey` stays for
fetching) and the label `role:imports`. The merger then adds `--imports <steps> --push-branch fletchery/round-N` to its
`machines.py run`. The node builds the editor, runs `Tools/CI/RunImports.ps1`, which commits the content and pushes it
with `PushWithLfs.ps1` to that branch only (the worker accepts no other branch, and PushWithLfs refuses main), then runs
the build, BuildContent and checks on that commit. The merger fast-forwards to the pushed commit and lands it. Only an
import job's steps get the write key.

### Two nodes share a round

When `run` finds two or more nodes free for `node-checks` and the merger passes a list of checks (`--only`), it splits
the list across the nodes (round-robin) and queues one job per node. Each node builds the candidate and runs its half
at the same time, and `run` waits for all of them: exit 1 if any half failed, 2 if any gave no result, 0 when every
half passed. A run with `--imports` is never split, because the content is imported and pushed once. With one node
free it is one job, as before.

The Gaming PC can be the second node: run `node_worker.py` on the PC with its own `workRoot` (not a Fletchery
worktree), its own machine key, and the same labels as SF-NODE-01. Its LifeShare scripts take the PC heavy lease
(LaneGuard), so a node job and a PC round never run Unreal at the same time. Pause it from Machines (Tests, PR checks,
Builds) when the PC is needed.

## Pausing one machine from one kind of work

Building → Machines shows a "Takes:" row of switches under each machine. Click one to stop that machine from that kind
of work; click again to resume. Running work always finishes.

- **This PC:** Coding (no new worker sessions), Merging (the same as Pause on the Merge page), PR checks (no PC checks),
  Builds (no scheduled builds start here).
- **A node:** PR checks, Tests (node-checks, nightly, checkout-only) and Builds (`package`). A paused node claims no job
  of that kind and is not counted as eligible, so PR checks go back to the PC and Automatic builds pick another machine
  or the PC. A queued PR check that no node can take any more is cancelled so the PC can check that PR.

Node pauses live in `machines.db` (meta `paused:<name>`); PC pauses in `state.json` (`pc_paused`). Both survive restarts.
The Overview's machine strip lists what is paused.

## Setting it up

1. **Profile** (live profile, `workers` block; needs Alex's OK):

   ```json
   "machines": {"listen": "100.83.43.111:8771", "shadow": true}
   ```

   Fletchery restarts with it; the log says `machines: worker API on 100.83.43.111:8771`.
2. **Add the node** in Building → Machines → *Add or revoke a machine*, or on the PC:
   `python <install>\fletchery\machines.py --state-dir <state> add SF-NODE-01`. Copy the key it prints.
3. **On the node**, put the key in a file only the worker's account can read (e.g. `C:\StoneNode\keys\fletchery_node_key`),
   copy `fletchery\node_worker.py` anywhere, and write its config:

   ```json
   {"coordinator": "http://100.83.43.111:8771", "machine": "SF-NODE-01",
    "keyFile": "C:\\StoneNode\\keys\\fletchery_node_key",
    "labels": ["os:windows", "ram:24", "unreal:5.8.4", "role:tests", "role:cook"],
    "workRoot": "E:\\buildlane-node\\_work",
    "repoUrl": "git@github.com:Stonefletcher-Productions-LLC/LifeShare.git",
    "gitSshKey": "C:\\StoneNode\\keys\\lifeshare_deploy",
    "engine": "E:\\UE_5.8",
    "pauseFile": "E:\\GitHubRunner\\PAUSED-NODE",
    "recipes": ["node-checks", "nightly-full", "checkout-only", "pr-check", "package"]}
   ```

   The checkout is `<workRoot>\fletchery\LifeShare` (its own, so it never fights the Buildlane runner's checkout).
   `python node_worker.py check --config <file>` signs in once and prints what the coordinator answers.
   `python node_worker.py install --config <file>` adds the logon task **Fletchery node worker** (current user,
   limited rights, below-normal priority); `schtasks /Run /TN "Fletchery node worker"` starts it now.
   The worker's own log is `<workRoot>\fletchery\worker.log`.
4. **Shadow run:** queue a `checkout-only`, then a `node-checks` job from the dashboard and compare with the
   Buildlane runner's result for the same commit.
5. **Cutover:** stop the Buildlane node runner, set `"shadow": false` and add `"nightly"`.

While `pauseFile` exists the node still checks in (it shows "paused on the node") but takes no new job.

## Command line

```text
python machines.py --state-dir <state> add <name>       # prints the key once
python machines.py --state-dir <state> revoke <name>
python machines.py --state-dir <state> list
python machines.py --state-dir <state> enqueue --recipe node-checks --sha <40 hex> [--only A,B] [--strict-slots] [--pr N]
python machines.py --state-dir <state> cancel <id>
```

A job queued from the command line gets its commit status from the backend within 10 seconds.

## The worker API

`POST`, JSON, `Authorization: Bearer <key>`, `X-Fletchery-Machine: <name>`; 1 MB per request.

| Path | Body | Answer |
|---|---|---|
| `/v1/hello` | `labels, ram_gb, version, stats` | `poll_seconds` |
| `/v1/claim` | `stats, paused` | `job` (`id, recipe, sha, ref, params, timeout_minutes`) or `null` |
| `/v1/heartbeat` | `job_id, stats` (every 30 s) | `cancel` |
| `/v1/log` | `job_id, text` (every 5 s) | `ok` |
| `/v1/result` | `job_id, exit_code, summary, result` (the result JSON) | `ok` |

Later (step 5): merge rounds split their checks across machines.
