# Writing project rules

Workers are good at following the code around them. Project rules tell them the things the code does not say: what
you call things, what must never change, and which quick checks prove a change is safe. Rules live in your project's
own repository, in a `.fletchery/` folder, so they change with the code they describe and go through review like
everything else.

```
your-project/
└── .fletchery/
    ├── conventions.md   read by every worker, reviewer and fixer
    ├── gates.json       quick checks Fletchery runs on each change
    ├── worker.md        optional: extra notes for workers
    ├── reviewer.md      optional: what the reviewer must look for
    ├── fixer.md         optional: how to read your build's failures
    └── planner.md       optional: how to split a big request into tasks
```

All of them are optional. With none, workers follow the code around them, and the reviewer and your Build Lane still
check the result.

## conventions.md

Plain Markdown, written for a new colleague. Keep it short and specific; every worker reads all of it before every
task, so a page is better than a book.

```markdown
# Harbor conventions

- Customers are "members" everywhere: code, UI text and docs. Never "users".
- Every text a member can see comes from `strings/en.json`; never write it inline in a component.
- Money is stored as whole cents (`int`), never floats.
- Do not touch `migrations/` that are already released; add a new migration instead.
- Tests sit next to the code: `thing.py` → `test_thing.py`.
```

Good rules are checkable: a reviewer can say yes or no. "Write clean code" is not a rule; "functions that touch the
database live in `store/`" is.

## gates.json

The checks Fletchery runs itself on every change, before the reviewer sees it. A change that fails a check goes back
to the worker with the output. Keep them quick (seconds, not minutes): the full build and tests belong to your Build
Lane.

```json
{
  "always": ["python tools/check_conventions.py"],
  "rules": [
    {"paths": "^strings/", "run": "python tools/check_strings.py"},
    {"paths": "^store/|^migrations/", "run": ["python -m pytest store -q", "python tools/check_migrations.py"]}
  ]
}
```

- `always` runs on every change.
- Each entry in `rules` runs when any changed file matches `paths`, a regular expression on the file's path from the
  repository root (forward slashes).
- `run` is one command or a list. Commands run from the change's folder; a check script the branch does not have yet
  runs from your project's main checkout.
- On Windows, write `python`, not `python3`: Fletchery runs Python checks with its own Python either way.

A good first gate is a small script that looks for your conventions' most common slips (a banned word, text written
inline) and prints the file and line of each.

## worker.md, reviewer.md, fixer.md, planner.md

Extra notes added to that role's instructions, after the conventions:

| File | Use it for |
|---|---|
| `worker.md` | How to work here: where to start reading, which commands are safe to run, what not to run (long builds) |
| `reviewer.md` | The few things a reviewer must always check, each with a short name so findings are easy to scan |
| `fixer.md` | How to read your build's failures: where the logs are, which errors are usually flaky |
| `planner.md` | How to split a big request into tasks: size, order, what each must include |

## Writing good tasks

Rules say how; the issue says what. A task a worker finishes on its own usually has:

- **What to change**, in a sentence or two.
- **Where**, if you know: a file, a screen, a function.
- **How to tell it worked**: what a person would see, or which test should pass.
- **What not to touch**, if there is a nearby trap.
- `Depends on: #12` on its own line if it must wait for another task.

Comments on the issue count too: every worker reads the issue and all its comments before starting, so answering a
question in a comment and re-queueing the task is the normal way to steer.
