<p align="center">
  <img src="docs/images/fletchery/icon.png" width="96" alt="">
</p>

<h1 align="center">Fletchery</h1>

<p align="center"><strong>Turn GitHub issues into finished, tested pull requests while you do something else.</strong></p>

Fletchery is a Windows app for one person and a team of AI coding agents (Claude, Codex, DeepSeek and others through
OpenCode). You write what you want as GitHub issues. An agent writes each change on its own branch, Fletchery checks
it, an agent from a different company reviews it, and it becomes a pull request. Your build lane then builds, tests
and merges it. One window shows all of it, and nothing merges without passing your checks.

![Fletchery with a queue of tasks, two workers writing code, and their pull requests (illustrative data)](docs/images/fletchery/overview.png)

<sub>Every screenshot here is the real app showing a made-up project, "Harbor". See [where the pictures come from](#where-the-pictures-come-from).</sub>

**Jump to:** [How it works](#how-it-works) · [Install](#install) · [First ten minutes](#first-ten-minutes) ·
[Everyday tasks](#everyday-tasks) · [Coding agents](#coding-agents) · [Add another project](#add-another-project) ·
[Settings](#settings) · [Costs and limits](#costs-and-limits) · [Project rules](#project-rules) ·
[Something looks wrong](#something-looks-wrong) · [For developers](#for-developers)

## How it works

```
 GitHub issues ──▶ Workers ──────────────────────────────▶ Pull request ──▶ Build Lane ──▶ merged
 (your tasks)     an agent writes it, Fletchery checks it,        ▲         builds and tests
                  another company's agent reviews it              │         on your PC
                                                                  └── fixer ◀── sent back
```

| Part | What you see | What it does |
|---|---|---|
| **Task queue** | Issues with the project's label, oldest first, the next one marked | Waits for a free worker. A task that says `Depends on: #12` waits until #12 is done |
| **Workers** | Each worker's task, which agent has it, its step (write, check, review, open PR) and the last thing it did | Agent sessions writing code on their own branches. They never build, merge or touch your other work |
| **Workers' PRs** and **Needs you** | The pull requests workers opened, and tasks that stopped for a decision | A PR the lane sends back goes to a fixer automatically, twice at most; after that it asks you |
| **Agents** | Each provider (Anthropic, OpenAI, OpenCode Go…) and its models, with scores and status | Picks the cheapest agent good enough for each task, and switches agents on or off |
| **Build Lane** | What is building, which step, how far, and the PRs waiting | Your own build machine, as before. Fletchery shows it and can pause it |

<p>
  <img src="docs/images/fletchery/workers.png" alt="Two workers: each shows its task, a four-step progress track and the file it is editing">
</p>

## Install

**You need:** Windows 10 or 11, [Python 3.10 or newer](https://www.python.org/downloads/) (tick *Add python.exe to PATH*),
[Git](https://git-scm.com/download/win), the [GitHub CLI](https://cli.github.com/) signed in (`gh auth login`), and at
least one coding agent signed in: [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) (run `claude`,
then `/login`), the Codex CLI (`codex login`) or [OpenCode](https://opencode.ai) (`opencode auth login`). Microsoft Edge's WebView2, which draws the window, is already part of Windows 10 and 11.

1. **Get the code:** clone this repository, or use **Code → Download ZIP** and extract it.
2. **Run the installer** in Windows PowerShell, in the repository folder:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\shell\Install-Fletchery.ps1
   ```

   It builds `Fletchery.exe` with the compiler that comes with Windows, installs the Claude Agent SDK for Python if it
   is missing, copies the app to `%LOCALAPPDATA%\Programs\Fletchery`, and adds **Fletchery** to the desktop and the
   Start menu. No administrator rights.

3. **Open Fletchery** and fill in your project in **Settings** ([First ten minutes](#first-ten-minutes)). To update later, pull this repository and run the installer again. Fletchery picks up the new
   version between tasks, never in the middle of one.

## First ten minutes

**1. Point it at your project.** Clone your repository somewhere on this PC first, for example
`git clone https://github.com/you/harbor.git C:\Code\harbor`. Then open **Settings** and fill in **This project**:

- **GitHub repository**: `owner/name`, or paste its GitHub link.
- **Local clone**: the folder you just cloned to. Fletchery makes each task its own worktree from it and never edits
  your checked-out files.
- **Base branch**: where pull requests go, usually `main`.
- **Task label**: issues with this label become tasks (`fletchery` by default).

Press **Check the project** to see that GitHub and the clone line up, then **Save**. Fletchery restarts once to pick
it up. If you have a Build Lane, fill in its status file, pause file and drive under **Build Lane**, and its panel
appears.

<p><img src="docs/images/fletchery/settings.png" width="80%" alt="Settings, This project: name, repository, local clone, base branch, task label, then worker and Build Lane settings"></p>

<sub>Prefer files? Everything here is saved in a small profile in `%APPDATA%\Fletchery\Projects`. See
[`examples/Example.profile.json`](examples/Example.profile.json).</sub>

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
| Run more or fewer workers at once | **−** / **+** next to *Workers* (0 to 6) |
| Stop new code but keep merging and building | Turn *Workers* down to **0**. Running sessions finish; the Build Lane carries on |
| Spend less | **−** next to the daily cap. At the cap no new work starts that day |
| See this week's usage | **This week** under the cap |
| Switch an agent between its plan and an API key | **Settings > Accounts**, *Runs on* next to the agent (applies to its next session) |
| Use Codex, DeepSeek or another model too | **Add agent** in the Agents panel, then **On** / **Off** per model ([Coding agents](#coding-agents)) |
| See why an agent gets the tasks it does | Open its row in the Agents panel for its scores |
| Send one task to a particular agent | An `agent:<name>` label, or `Agent: <name>` on its own line in the issue |
| Store an API key for an agent | **Settings > Accounts > API keys** (kept in Windows Credential Manager) |
| Sign an agent in again | **Settings > Accounts**, **Sign in** next to its provider |
| Work on another repository | **Settings > Projects** ([Add another project](#add-another-project)) |
| Stop builds on the build PC | **Pause builds** in the Build Lane panel. A build already running finishes |
| Close the window but keep working | Close it. Fletchery stays in the tray; right-click the tray icon to quit |
| Make the task list taller | Drag the handle under the Task queue (or focus it and use the arrow keys) |

## Coding agents

The **Agents** panel groups agents by where they come from: **Anthropic** (Claude Code), **OpenAI** (Codex CLI) and
each OpenCode provider, such as **OpenCode Go** for DeepSeek or Haiku. Each model is one row with its overall score, its
status and an **On / Off** switch.

![The Agents panel: Anthropic with opus and sonnet, OpenAI with two Codex models switched off, OpenCode Go with DeepSeek and Haiku; opus is open to show its scores](docs/images/fletchery/agents.png)

**How it picks.** Each task gets a kind (C++ / Unreal, Python & tools, UI, Docs & data) and a difficulty (easy,
medium, hard). Each difficulty has a bar. The cheapest agent that is on, signed in, not at a limit and scores above the
bar gets the task. The review goes to the best reviewer from a different company than the writer. If an agent fails,
the task moves up to a stronger one before it asks you.

**Where the scores come from.** They start from public coding benchmarks for the model, then move with how the agent
actually does on your tasks. Open a row to see each score and where it came from, and use **Edit scores** to set one
yourself. **Refresh benchmarks** fetches the latest list. To send one task to a particular agent, give its issue an
`agent:<name>` label.

**Adding a model.** Press **Add agent** and type the model (for example `opencode-go/claude-haiku-5-5`). The name,
company and scores fill in by themselves. [Coding agents](docs/AGENTS.md) has the details.

**Accounts.** Sign-ins, plan or API key, and API keys are in **Settings > Accounts** (or **Accounts & keys** on the
panel). Models on one sign-in share its limits, so when OpenCode Go hits its limit, all its models pause together and
the others carry on.

<p><img src="docs/images/fletchery/accounts.png" width="80%" alt="Settings, Accounts: each provider with a Sign in button, and each agent's status and whether it runs on the plan or an API key"></p>

## Add another project

Fletchery works on one project at a time and remembers all of them.

1. Clone the new repository on this PC, for example `git clone https://github.com/you/desk-app.git C:\Code\desk-app`.
2. Open **Settings > Projects** and fill in **Add a project**: a name, the repository, the clone folder, the base
   branch and the task label.
3. Press **Add project**. Fletchery checks that GitHub and the clone match, and creates the task labels on GitHub
   (`fletchery`, `fletchery:queued`, `:running`, `:pr`, `:blocked`). The new project starts with one worker and a $20
   daily cap, and can copy your agents.
4. Press **Switch to it**. Running sessions finish first, then Fletchery opens the other project. Switch back the same
   way.

<p><img src="docs/images/fletchery/projects.png" width="80%" alt="Settings, Projects: the list of projects with Switch to it, and the Add a project form"></p>

Optional: give the repository a `.fletchery/` folder with its own rules ([Project rules](#project-rules)).

## Settings

**Settings** (top of the window) has three tabs:

| Tab | What's there |
|---|---|
| **This project** | Repository, clone, base branch, label; workers at once, daily cap, how often to check GitHub, fix attempts, code freeze, escalation; the Build Lane's files. Less common settings sit under *More…*. A setting marked *(restart)* applies after Fletchery restarts, which it offers to do |
| **Accounts** | Each provider's sign-in, whether each agent runs on its plan or an API key, and the API keys |
| **Projects** | Your projects, **Switch to it**, and **Add a project** |

## Costs and limits

- **On a plan** (the default), each agent uses your own sign-in: your Claude plan, ChatGPT for Codex, or OpenCode Go.
  Its usage counts against that plan's limits, and the amounts Fletchery shows are estimates, not a bill. When a limit
  is reached, that agent (and every model on the same sign-in) pauses by itself, and its task goes back in the queue
  with its work kept.
- **On an API key**, store the key in **Settings > Accounts** and switch that agent's *Runs on* to **API key**.
  Sessions are billed per token, and the amounts shown are what they cost.
- Two guards apply either way: a **daily cap** (no new work after it) and a **per-session budget**. If a sign-in
  expires, that agent pauses and its provider shows **Not signed in · sign in**.

<p><img src="docs/images/fletchery/usage-week.png" width="70%" alt="Usage over the last seven days as columns, with the daily cap marked and the same numbers as a table"></p>

A limit on one account pauses only the agents on it, and their tasks go to the others.

## Project rules

Each project can tell its workers how it likes things done, in a `.fletchery/` folder in its own repository:

| File | What it does |
|---|---|
| `conventions.md` | Read by every worker, reviewer and fixer: naming, style, what not to touch |
| `gates.json` | The quick checks Fletchery runs on every change, chosen by which files changed |
| `worker.md`, `reviewer.md`, `fixer.md` | Extra notes for that role |
| `merger.md` | How a merge round builds, imports and tests (see Merge rounds below) |

[Writing project rules](docs/WORKERS.md) has examples. With no rules, workers follow the code around them, and the
reviewer and your Build Lane still check the result.

## The Build Lane

If you run a build machine (the [local runner](runner/README.md), or your own CI writing a small status file), Fletchery
shows it under the workers: what is building, which step, how far, and the PRs waiting their turn. Pull requests from
workers are marked.

![The Build Lane panel: a batch of two PRs in its Tests step, 9 of 14 done, and the PRs waiting](docs/images/fletchery/build-lane.png)

Releases, the integration mode, timing history and merge controls are still in the classic BuildLane app for now
([its guide](docs/BUILDLANE-CLASSIC.md)); they are moving into Fletchery next.

## Merge rounds

Turn on **Settings > Merging > Merge rounds** and Fletchery keeps merging by itself. Whenever pull requests labelled
`ready` wait on the base branch and no round is running, it starts one: an agent in its own worktree merges them
together, builds and tests them the way `.fletchery/merger.md` says (or the file in **Round instructions file**), leaves
out any PR it can't make pass with a comment saying why, and lands the rest through one "Merge round N" PR that it merges
itself. Nobody is asked for approval: turning rounds on is the approval. Workers at 0 don't stop rounds.

The Merge section shows the round's steps, the last result, and **Pause merging** / **Merge now**. A round that landed
nothing waits **Retry a failed round after** minutes before trying the same PRs again, unless one of them changes.
`examples/LifeShare.merger.md` is a full example of round instructions.

## Safe by design

- **Your machine only:** the screens are served on `127.0.0.1`, and every button needs a key that only the page
  Fletchery itself served knows.
- **Branches only:** workers work on their own branches in their own folders. They never push to your main branches,
  never merge and never force-push.
- **Reviewable:** every change is a pull request with the checks it passed and what could not be verified.
- **No hidden credentials:** Fletchery uses your `gh` and agent sign-ins. API keys are kept in Windows Credential
  Manager, never in a profile, the repository or the log, and the app shows only their last four characters.

## Something looks wrong

| You see | What to do |
|---|---|
| "Not signed in" on a provider | **Settings > Accounts**, **Sign in**, then **Check sign-ins**. Work resumes on the next check |
| An agent says "Paused until…" | It hit a usage limit. Its tasks go to the other agents; it comes back by itself. Upgraded the plan? Press **Refresh agents** (or the chip) to lift the pause now |
| Nothing new starts | Check *Workers* is not at 0, the daily cap is not reached, and **Pause** is not on |
| "Can't reach Fletchery's backend" | The app restarts it by itself. If it stays, quit from the tray and reopen; the log is under **Open log folder** in the tray menu |
| A task keeps landing under **Needs you** | Read the last comment on its issue: it says what a person must decide |
| The Build Lane panel is missing | Fill in the Build Lane's status file (and pause file, drive) in **Settings** |
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
