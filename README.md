<p align="center">
  <img src="docs/images/fletchery/icon.png" width="96" alt="">
</p>

<h1 align="center">Fletchery</h1>

<p align="center"><strong>Turn GitHub issues into finished, tested pull requests while you do something else.</strong></p>

Fletchery is a Windows app for one person and a team of Claude workers. You write what you want as GitHub issues.
Claude writes each change on its own branch, Fletchery checks it, a second Claude reviews it, and it becomes a pull
request. Your build lane then builds, tests and merges it. One window shows all of it, and nothing merges without passing
your checks.

![Fletchery with a queue of tasks, two workers writing code, and their pull requests (illustrative data)](docs/images/fletchery/overview.png)

<sub>Every screenshot here is the real app showing a made-up project, "Harbor". See [where the pictures come from](#where-the-pictures-come-from).</sub>

**Jump to:** [How it works](#how-it-works) · [Install](#install) · [First ten minutes](#first-ten-minutes) ·
[Everyday tasks](#everyday-tasks) · [Costs and limits](#costs-and-limits) · [Project rules](#project-rules) ·
[Something looks wrong](#something-looks-wrong) · [For developers](#for-developers)

## How it works

```
 GitHub issues ──▶ Workers ──────────────────────────────▶ Pull request ──▶ Build Lane ──▶ merged
 (your tasks)     Claude writes it, Fletchery checks it,          ▲         builds and tests
                  a second Claude reviews it                      │         on your PC
                                                                  └── fixer ◀── sent back
```

| Part | What you see | What it does |
|---|---|---|
| **Task queue** | Issues with the project's label, oldest first, the next one marked | Waits for a free worker. A task that says `Depends on: #12` waits until #12 is done |
| **Workers** | Each worker's task, its step (write, check, review, open PR) and the last thing it did | Claude sessions writing code on their own branches. They never build, merge or touch your other work |
| **Workers' PRs** and **Needs you** | The pull requests workers opened, and tasks that stopped for a decision | A PR the lane sends back goes to a fixer automatically, twice at most; after that it asks you |
| **Build Lane** | What is building, which step, how far, and the PRs waiting | Your own build machine, as before. Fletchery shows it and can pause it |

<p>
  <img src="docs/images/fletchery/workers.png" alt="Two workers: each shows its task, a four-step progress track and the file it is editing">
</p>

## Install

**You need:** Windows 10 or 11, [Python 3.10 or newer](https://www.python.org/downloads/) (tick *Add python.exe to PATH*),
[Git](https://git-scm.com/download/win), the [GitHub CLI](https://cli.github.com/) signed in (`gh auth login`), and
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview) signed in to your Claude plan (run `claude`, then
`/login`). Microsoft Edge's WebView2, which draws the window, is already part of Windows 10 and 11.

1. **Get the code:** clone this repository, or use **Code → Download ZIP** and extract it.
2. **Describe your project** in a profile. Copy [`examples/Example.profile.json`](examples/Example.profile.json) and fill
   in the repository and the folder of a local clone; [First ten minutes](#first-ten-minutes) walks through it.
3. **Run the installer** in Windows PowerShell, in the repository folder:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\shell\Install-Fletchery.ps1 -Profile C:\path\to\MyProject.profile.json
   ```

   It builds `Fletchery.exe` with the compiler that comes with Windows, installs the Claude Agent SDK for Python if it
   is missing, copies the app to `%LOCALAPPDATA%\Programs\Fletchery`, and adds **Fletchery** to the desktop and the
   Start menu. No administrator rights.

4. **Open Fletchery.** To update later, pull this repository and run the installer again. Fletchery picks up the new
   version between tasks, never in the middle of one.

## First ten minutes

**1. The profile.** One small file per project:

```json
{
  "name": "Harbor",
  "repo": "example/harbor",
  "workers": {
    "projectRoot": "C:\\Code\\harbor",
    "baseBranch": "main",
    "label": "fletchery",
    "maxParallel": 2,
    "dailyBudgetUsd": 20
  }
}
```

- `repo` is `owner/name` from the GitHub address.
- `projectRoot` is a local clone that Fletchery can make branches from; it never edits your checked-out files.
- Issues with `label` are tasks.
- `baseBranch` is where pull requests go.
- If you also have a Build Lane, add its `statusFile`, `pauseFile` and `drive` (the same fields the classic app uses), and the Build Lane panel appears.

**2. Give it a task.** Press **Add task** (or label any issue `fletchery` on GitHub). Write it like you would for a
colleague: what to change, what not to touch, and how to tell it worked.

<p><img src="docs/images/fletchery/add-task.png" width="70%" alt="The Add a task dialog: a title, what to do, and which issues it waits for"></p>

**3. Watch it.** Within a minute a worker takes it. The **Workers** section shows its step and the file it is working
on; when it is done, a pull request appears under **Workers' PRs**, and Windows tells you.

**4. Steer it.** Comment on the task's issue: every worker reads the issue and all its comments before it starts. If a
task lands under **Needs you**, its issue says what it needs; answer in a comment, then change its label from
`fletchery:blocked` to `fletchery:queued`.

## Everyday tasks

| I want to… | Do this |
|---|---|
| Add work | **Add task**, or label an issue on GitHub. `Depends on: #12` in the description makes it wait for #12 |
| Stop new work for a while | **Pause** (top right). Workers already running finish; nothing new starts until **Resume** |
| Run more or fewer workers at once | **−** / **+** next to *Workers* (1 to 6) |
| Spend less | **−** next to the daily cap. At the cap no new work starts that day |
| See this week's usage | **This week** under the cap |
| Switch between my Claude plan and an API key | **Claude plan** / **API key** under the cap (applies to the next session) |
| Use Codex, DeepSeek or another agent too | Add `agents` to the profile ([Coding agents](docs/AGENTS.md)); then **On** / **Off** per agent in the Agents panel |
| Send one task to a particular agent | An `agent:<name>` label, or `Agent: <name>` on its own line in the issue |
| Store an API key for an agent | **Set** next to the key in the Agents panel (kept in Windows Credential Manager) |
| Stop builds on the build PC | **Pause builds** in the Build Lane panel. A build already running finishes |
| Close the window but keep working | Close it. Fletchery stays in the tray; right-click the tray icon to quit |
| Make the task list taller | Drag the handle under the Task queue (or focus it and use the arrow keys) |

## Costs and limits

- **On your Claude plan** (the default), workers use the same plan as your own Claude chats, so their usage counts
  against the plan's limits. The amounts Fletchery shows are estimates, not a bill. When a limit is reached, Fletchery
  pauses by itself, and the task goes back in the queue with its work kept.
- **On an API key**, set `ANTHROPIC_API_KEY` for your Windows user and switch to **API key**. Sessions are billed per
  token, and the amounts shown are what they cost.
- Two guards apply either way: a **daily cap** (no new work after it) and a **per-session budget**. If Claude's sign-in
  expires, Fletchery pauses and tells you to run `claude` and `/login`.

<p><img src="docs/images/fletchery/usage-week.png" width="70%" alt="Usage over the last seven days as columns, with the daily cap marked and the same numbers as a table"></p>

Each agent in [Coding agents](docs/AGENTS.md) runs on its own plan or key, so its limits are its own. A limit on one
agent pauses that agent only, and its tasks go to the others.

## Project rules

Each project can tell its workers how it likes things done, in a `.fletchery/` folder in its own repository:

| File | What it does |
|---|---|
| `conventions.md` | Read by every worker, reviewer and fixer: naming, style, what not to touch |
| `gates.json` | The quick checks Fletchery runs on every change, chosen by which files changed |
| `worker.md`, `reviewer.md`, `fixer.md` | Extra notes for that role |

[Writing project rules](docs/WORKERS.md) has examples. With no rules, workers follow the code around them, and the
reviewer and your Build Lane still check the result.

## The Build Lane

If you run a build machine (the [local runner](runner/README.md), or your own CI writing a small status file), Fletchery
shows it under the workers: what is building, which step, how far, and the PRs waiting their turn. Pull requests from
workers are marked.

![The Build Lane panel: a batch of two PRs in its Tests step, 9 of 14 done, and the PRs waiting](docs/images/fletchery/build-lane.png)

Releases, the integration mode, timing history and merge controls are still in the classic BuildLane app for now
([its guide](docs/BUILDLANE-CLASSIC.md)); they are moving into Fletchery next.

## Safe by design

- **Your machine only:** the screens are served on `127.0.0.1`, and every button needs a key that only the page
  Fletchery itself served knows.
- **Branches only:** workers work on their own branches in their own folders. They never push to your main branches,
  never merge and never force-push.
- **Reviewable:** every change is a pull request with the checks it passed and what could not be verified.
- **No hidden credentials:** Fletchery uses your `gh` and `claude` sign-ins. An API key stays in your Windows user's
  environment, never in a profile.

## Something looks wrong

| You see | What to do |
|---|---|
| "Claude needs signing in again" | Open a terminal, run `claude`, type `/login`. Work resumes on the next check |
| "Can't reach Fletchery's backend" | The app restarts it by itself. If it stays, quit from the tray and reopen; the log is under **Open log folder** in the tray menu |
| A task keeps landing under **Needs you** | Read the last comment on its issue: it says what a person must decide |
| The Build Lane panel is missing | Add `statusFile` (and `pauseFile`, `drive`) to the profile |
| Fletchery did not start: Python | Install Python 3.10+ from python.org with *Add to PATH*, then run the installer again |

## For developers

| Folder | What |
|---|---|
| `fletchery/` | The backend (Python): task graph, workers, checks, review, fixers, the local server. `python -m fletchery demo --serve --fixture harbor` runs it on made-up data |
| `ui/` | The screens (one page, no build step) |
| `shell/` | `Fletchery.exe` (C# + WebView2) and its build and install scripts |
| `app/`, `runner/` | The classic BuildLane app and the local GitHub Actions runner |

Tests: `python fletchery/test_fletchery.py` (no network). The classic app keeps its own Windows checks
(`.github/workflows/validate.yml`). Design and roadmap: [docs/FLETCHERY.md](docs/FLETCHERY.md).
[Contributing](CONTRIBUTING.md).

### Where the pictures come from

[`docs/images/fletchery/Make-Screenshots.ps1`](docs/images/fletchery/Make-Screenshots.ps1) runs Fletchery on the
made-up Harbor project and photographs each screen with Microsoft Edge. They are real screens with illustrative data,
not results from a real project.

---

Made by Stonefletcher Productions LLC. Fletchery grew out of BuildLane: the GitHub address still works under the old
name.
