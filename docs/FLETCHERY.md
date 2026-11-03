# Fletchery

Fletchery is what BuildLane becomes: one Windows app for the whole path a change takes.

```
GitHub issues ──▶ Workers (Claude writes the change, checks, a second Claude reviews) ──▶ PR
      ▲                                                                                  │
      └──── fixers answer what the lane sends back ◀── Build Lane (build, test, merge) ◀──┘
```

The name: a fletchery is the workshop where raw shafts become finished arrows. Inside it the parts keep their names:
**Task queue**, **Workers**, **Build Lane**, **Releases**.

## Decisions (Alex, 2026-11-02)

- One product and one repo (this one). The Workers engine, born in LifeShare as "Foreman", moves here as a feature any
  project can turn on. A project keeps only its own rules (conventions, which checks to run, prompt additions) in its
  own repository.
- A native Windows app: `Fletchery.exe` owns the window, taskbar and tray icon, notifications and single-instance
  behaviour, and draws its screens with WebView2 (the Edge engine built into Windows 10 and 11). Screens are one web UI,
  served by the local backend on 127.0.0.1 only.

## Layout

| Folder | What |
|---|---|
| `fletchery/` | The backend (Python 3.10+): the local server, the Workers engine, the Build Lane reader and controls, GitHub reads |
| `ui/` | The screens: one page, tabbed (Work, Build Lane, Releases, Integration, Timing, System, Projects, Settings) |
| `shell/` | `Fletchery.exe` (C#, built with the .NET Framework compiler in Windows, like today's launcher) and its installer |
| `runner/` | The local GitHub Actions runner, unchanged |
| `app/` | Today's Windows Forms app. Kept, and still installed, until the new screens reach parity; then retired |

A project profile (today's `*.profile.json`) gains a `workers` block: base branch, labels, worker count, budgets, auth,
and `rules`: a folder in the project's own checkout with `conventions.md`, `gates.json` (which checks run for which
changed paths) and optional prompt additions. LifeShare's live in `LifeShare/.fletchery/`.

## Phases

1. **One window.** `Fletchery.exe` with the Work screen (task queue, workers, Foreman PRs, needs-you), the Build Lane
   panel and the cost controls; the Workers engine moved here and made project-generic; LifeShare's rules moved to its
   repo; installed on Alex's PC next to the old app.
2. **Parity.** Port Overview's PR list and merge controls, Releases, Integration, Timing history, System and Projects
   into the web UI, test against the same fixtures the PowerShell tests use; then retire `app/`.
3. **Ship.** MSI with the shell, backend and UI (Python embeddable runtime bundled, so no system Python), docs and
   screenshots, rename this repository to Fletchery (GitHub keeps the old URL working).

## Rules carried over

Localhost only; every button needs the page's per-run token; colour-blind-safe colours with a glyph and a word for
every state; no secrets in profiles (tokens stay in Windows DPAPI files per repository); never stop a process by name;
build jobs and editors are the lane's, never the app's.
