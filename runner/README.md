# Buildlane local runner

The local runner runs a repository's own GitHub Actions workflows on your PC, so the project does not
need GitHub-hosted Actions (or Actions at all). GitHub stays where the code, pull requests, reviews and
merges live. The runner watches it, decides what each change starts, runs the jobs here, and reports
back as commit statuses.

It reads the same `.github/workflows/*.yml` files GitHub would. You do not keep a second pipeline.

## What it does

- **Triggers.** It checks GitHub every minute (`pollSeconds`). Supported triggers:
  - `push`, with `branches` and `paths` filters
  - `pull_request` (opened, synchronize, reopened, ready_for_review, labeled)
  - `pull_request_target`
  - `schedule` (cron in UTC; a missed minute fires once, not as a backlog)
  - `workflow_run` (requested, in_progress, completed)
  - `workflow_dispatch`
- **Jobs.** The runner handles these workflow features:
  - Ordering and conditions: `needs`, `if:` (including `success()`, `failure()`, `always()` and `cancelled()`), `strategy.matrix`
  - Outputs and environment: job outputs, `env` at every level, `defaults.run`
  - Step settings: `timeout-minutes`, `continue-on-error`, `concurrency` (`cancel-in-progress` and `queue: max`)
  - Run names: `run-name`
  - Expressions: the full expression language
  - Files and commands: `GITHUB_OUTPUT`, `GITHUB_ENV`, `GITHUB_PATH`, `GITHUB_STEP_SUMMARY` and the `::command::` lines
- **Shells.** These shells are supported: `powershell`, `pwsh`, `bash` (Git Bash on Windows), `sh`,
  `python` and `cmd`, plus custom `{0}` shells. Jobs written for `ubuntu-latest` run in Git Bash. A
  `python3` shim points at `python`.
- **Actions.**
  - JavaScript and composite actions are fetched once into `<root>\_actions`, so `actions/checkout`
    works as it does on GitHub.
  - `actions/github-script` ships with the runner (same inputs and outputs, no download).
  - `upload-artifact` and `download-artifact` keep artifacts in the local run store.
  - `setup-*` use the tools already installed on the PC.
  - `cache` always misses.
- **A local GitHub API.** Jobs get `GITHUB_API_URL` pointing at the runner (`127.0.0.1`).
  - Most calls are forwarded to GitHub unchanged.
  - The runner answers the Actions endpoints itself: workflow dispatch, runs, run attempts, jobs, logs,
    artifacts, cancel and re-run. Scripts that dispatch a workflow, or check a run's identity and
    steps, keep working.
  - `commits/{ref}/check-runs` also lists the local jobs as check runs (app id `checkAppId`, default
    15368, the GitHub Actions app). Merge policies that require named checks see local results.
- **Statuses.** Every job posts a commit status (pending, then success or failure) on the commit it
  tested, under the job's name. For a pull request, that commit is the PR head.
- **Commands from anywhere.** Comment on the `commandIssue` with one command per line:
  - `/buildlane run <workflow.yml> [ref=<branch>] [name=value ...]`
  - `/buildlane cancel <run>`
  - `/buildlane rerun <run> [failed]`
  - `/buildlane status`

  Only logins in `dispatchers` are obeyed. The run's actor is the comment's author, so workflow
  policies that trust specific people still apply.
- **Process safety.** Child processes run at below-normal priority. Cancelling a job or hitting its
  timeout stops that job's own process tree, never other processes.

## Shadow mode first

A new config starts with `"mode": "shadow"`. In shadow mode:

- The local API refuses every GitHub write (POST, PUT, PATCH, DELETE and GraphQL mutations) outside
  the Actions endpoints. A trial run cannot merge, push a ref or edit an issue.
- Pools marked `"shadow": false` (heavy build and lane pools) are skipped.
- Statuses are posted as `buildlane-shadow / <job>`, so they never satisfy a required check.
- Command replies are only logged.

Once its results match GitHub's, set `"mode": "live"` and turn off the GitHub workflows (see
Cutover).

## Install (Windows)

You need Node.js 18 or newer, Git for Windows, and the GitHub sign-in already saved by Buildlane.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File runner\Install-LocalRunner.ps1 -Start
```

The script does three things:

1. It copies the runner to `%USERPROFILE%\buildlane-local\runner`.
2. If you have no config yet, it writes `%USERPROFILE%\buildlane-local\runner.json`, a copy of
   `runner.example.json`.
3. It registers a scheduled task, *Buildlane local runner*, that starts at logon and restarts if it stops.

Remove the task with `-Uninstall`.

Everything lives outside AppData on purpose. Apps packaged as MSIX, Claude desktop among them, see a private copy of AppData. A config written from inside one would be invisible to the scheduled task. The installer warns you when it runs in such a shell.

Jobs written for Linux runners (`ubuntu-*` labels) get LF checkouts in their own work folder (`_work-linux`). Git for Windows otherwise converts line endings to CRLF.

## Config (`runner.json`)

| Key | Meaning |
| --- | --- |
| `repo`, `repoAliases` | `owner/name`, plus older names that GitHub still redirects |
| `root` | Run store, logs, action cache, poll state (`runs\<id>\run.json`, `runs\<id>\logs\<job>.log`) |
| `mode` | `shadow` (read-only trial) or `live` |
| `tokenFile` | File, or list of files, holding the GitHub token. Buildlane's saved sign-in is DPAPI-protected and read for the signed-in user. `BUILDLANE_TOKEN`, `GH_TOKEN` and `gh auth token` also work |
| `pools` | Runner pools. A job goes to the smallest pool whose `labels` contain all of its `runs-on` labels (`self-hosted` is ignored). `max` sets how many jobs run in parallel. `workRoot` sets where workspaces live; reuse an old runner's `_work` folder to keep incremental builds warm. `workspace` pins the folder exactly. `shadow: false` skips the pool in shadow mode, and `pausable: true` honours `pauseFile`. `jobs: ["ci.yml:quick"]` makes a pool take only those jobs, ahead of the label match, so one light job can run while its usual pool is `disabled` |
| `workflows`, `skipWorkflows` | Allow-list or deny-list of workflow file names |
| `commandIssue`, `dispatchers` | Issue number for `/buildlane` commands, and who may send them |
| `checkAppId` | App id that local check runs report (default 15368) |
| `postStatuses`, `statusPrefix`, `statusUrl` | Commit status settings |
| `secrets`, `secretFiles`, `vars`, `env` | Values for `secrets.*`, `vars.*` and extra environment for every job |
| `bashPath`, `pwshPath`, `pythonPath`, `nodePath` | Tool paths when they are not on PATH |

## Everyday use

```powershell
$R = "$env:USERPROFILE\buildlane-local\runner\buildlane-runner.cjs"
node $R list                                  # unfinished runs, then the 20 most recent (--limit N, --active for unfinished only)
node $R show <run>                            # jobs and steps
node $R log <run> [job]                       # logs
node $R run lane.yml task=resume              # queue a manual run (workflow_dispatch)
node $R cancel <run>
node $R rerun <run> --failed
node $R hold staging-controller.yml 15            # no schedule/workflow_run starts for 15 min (manual runs still start)
node $R unhold staging-controller.yml
node $R exec .github\workflows\lane-regression.yml --job regression --skip-checkout   # try one workflow now, in this folder
```

The service writes `runner-status.json` in `root` every few seconds, with recent runs, jobs and the
current step. `runner.log` records every decision it makes.

## Cutover from GitHub Actions

1. Run in shadow mode next to GitHub for a day. Compare `node $R list` with the Actions tab.
2. Set `"mode": "live"` and restart the task.
3. Disable the workflows on GitHub: **Settings → Actions → General → Disable actions**, or disable
   them one by one in the Actions tab. Otherwise both run.
4. If branch protection requires checks by name, the commit statuses the runner posts carry the same
   names. A rule that pins a check to the GitHub Actions app must be changed to accept any source.

## Limits

- Docker and container actions, `services:` and reusable workflows (`uses:` at job level) are not supported.
- Jobs run as the Windows user who runs the task, not as SYSTEM.
- Re-running a run keeps earlier attempts. Their logs and artifacts stay in the run folder.
- Logs stay on the PC. The GitHub status links to `statusUrl` if one is set.

## Tests

```powershell
node runner\test\TestRunner.cjs
```

The tests cover the expression engine, cron, filters and zip handling, plus an end-to-end run against a
fake GitHub: push, PR, schedule with cancel-in-progress, `workflow_run`, a dispatch from a job and from
a comment, artifacts through the local API, local check runs, statuses and shadow-mode write refusal.
