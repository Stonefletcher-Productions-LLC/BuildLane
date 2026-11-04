"""GitHub access through `gh api` (REST only: the lane's helpers and this session's proxy both avoid GraphQL),
and an in-memory fake with the same surface for tests and `--dry-run`."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import graph
from graph import Task


@dataclass
class PullRequest:
    number: int
    title: str
    head: str
    base: str
    head_sha: str
    labels: list[str]
    body: str = ""
    mergeable: bool | None = None


@dataclass
class Feedback:
    """Something the lane, the controller or CI said about a PR that a fixer should act on."""
    pr: int
    kind: str  # "comment" | "check" | "conflict"
    text: str
    at: str


def parse_pages(text: str):
    """`gh api --paginate` prints one JSON document per page back to back (`[...][...]`). Decode them one at a time:
    splitting on "][" breaks on any body that contains it, like a markdown link `[a][b]`."""
    dec = json.JSONDecoder()
    docs, i, text = [], 0, text.strip()
    while i < len(text):
        doc, i = dec.raw_decode(text, i)
        docs.append(doc)
        while i < len(text) and text[i].isspace():
            i += 1
    if not docs:
        return None
    if all(isinstance(d, list) for d in docs):
        return [x for d in docs for x in d]
    return docs[0] if len(docs) == 1 else docs


class GitHub:
    def __init__(self, repo: str, gh: str = "gh"):
        self.repo = repo
        self.gh = gh

    # ---- raw ----
    def api(self, path: str, method: str = "GET", body: dict | None = None, paginate: bool = False):
        cmd = [self.gh, "api", "-X", method, path]
        if paginate:
            cmd.append("--paginate")
        if body is not None:
            cmd += ["--input", "-"]
        # gh prints UTF-8; Windows' default (cp1252) can't decode some bytes and the whole pass failed (2026-11-03)
        p = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"gh api {method} {path}: {p.stderr.strip() or p.stdout.strip()}")
        return parse_pages(p.stdout) if paginate else (json.loads(p.stdout) if p.stdout.strip() else None)

    def api_text(self, path: str) -> str:
        """A GET whose answer is plain text (a job's log)."""
        p = subprocess.run([self.gh, "api", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"gh api {path}: {p.stderr.strip()}")
        return p.stdout

    # ---- issues ----
    def open_tasks(self) -> dict[int, Task]:
        items = self.api(f"repos/{self.repo}/issues?state=open&labels={graph.PREFIX}&per_page=100", paginate=True) or []
        tasks = {}
        for it in items:
            if it.get("pull_request"):
                continue
            tasks[it["number"]] = Task(it["number"], it["title"], it.get("body") or "",
                                       [l["name"] for l in it["labels"]], False, it["created_at"])
        return tasks

    def issue_closed(self, number: int) -> bool:
        it = self.api(f"repos/{self.repo}/issues/{number}")
        return it is not None and it["state"] == "closed"

    def set_status(self, number: int, status: str) -> None:
        """`status` is a plain state (queued, running, pr, blocked); the issue gets "<label>:<state>"."""
        it = self.api(f"repos/{self.repo}/issues/{number}")
        keep = [l["name"] for l in it["labels"] if not graph.is_status_label(l["name"])]
        self.api(f"repos/{self.repo}/issues/{number}", "PATCH", {"labels": keep + [graph.label_for(status)]})

    def comment(self, number: int, text: str) -> None:
        self.api(f"repos/{self.repo}/issues/{number}/comments", "POST", {"body": text})

    def issue_notes(self, number: int) -> list[tuple[str, str, str]]:
        """(author, created_at, body) of every comment a person left on the issue, oldest first. Fletchery's own
        comments are left out; they are status, not instructions."""
        items = self.api(f"repos/{self.repo}/issues/{number}/comments?per_page=100", paginate=True) or []
        return [(c["user"]["login"], c["created_at"], c.get("body") or "") for c in items if not _is_foreman(c.get("body") or "")]

    def issue_thread(self, number: int) -> dict:
        """The issue as a person reads it: title, body, labels and every comment (Fletchery's own marked), for the
        dashboard's reader so an answer doesn't need a trip to GitHub."""
        it = self.api(f"repos/{self.repo}/issues/{number}") or {}
        items = self.api(f"repos/{self.repo}/issues/{number}/comments?per_page=100", paginate=True) or []
        return {"number": number, "title": it.get("title", ""), "body": it.get("body") or "", "state": it.get("state", ""),
                "labels": [l["name"] for l in it.get("labels", [])], "url": it.get("html_url", ""),
                "comments": [{"author": c["user"]["login"], "at": c["created_at"], "body": c.get("body") or "",
                              "fletchery": _is_foreman(c.get("body") or "")} for c in items]}

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        it = self.api(f"repos/{self.repo}/issues", "POST", {"title": title, "body": body, "labels": labels})
        return it["number"]

    def close_issue(self, number: int) -> None:
        self.api(f"repos/{self.repo}/issues/{number}", "PATCH", {"state": "closed", "state_reason": "completed"})

    def edit_body(self, number: int, body: str) -> None:
        self.api(f"repos/{self.repo}/issues/{number}", "PATCH", {"body": body})

    def marked_issues(self, marker: str) -> list[dict]:
        # Include closed issues: a crash after creation must not recreate a completed prerequisite.
        items = self.api(f"repos/{self.repo}/issues?state=all&labels={graph.PREFIX}&per_page=100", paginate=True) or []
        return [i for i in items if not i.get("pull_request") and marker in (i.get("body") or "")]

    def merged_pr(self, branch: str) -> tuple[int, str, str] | None:
        """(number, base, merged_at) of the merged PR from `branch`, or None. A merge into a branch other than the
        default one (LifeShare's Test-Staging) does not close the issue its "Closes #n" names."""
        owner = self.repo.split("/")[0]
        items = self.api(f"repos/{self.repo}/pulls?state=closed&head={owner}:{branch}&per_page=10") or []
        for p in items:
            if p.get("merged_at"):
                return p["number"], p["base"]["ref"], p["merged_at"]
        return None

    def compare_commits(self, base: str, head: str) -> list[dict]:
        """Commits in `head` that `base` does not have, oldest first: sha, message, author."""
        d = self.api(f"repos/{self.repo}/compare/{base}...{head}") or {}
        return [{"sha": c["sha"], "message": c["commit"]["message"],
                 "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]}
                for c in d.get("commits", [])]

    # ---- status issue ----
    def status_issue(self) -> int | None:
        items = self.api(f"repos/{self.repo}/issues?state=open&labels={graph.PREFIX}-status&per_page=5") or []
        items = [i for i in items if not i.get("pull_request")]
        return items[0]["number"] if items else None

    def write_status(self, text: str) -> None:
        n = self.status_issue()
        if n is None:
            self.api(f"repos/{self.repo}/issues", "POST",
                     {"title": "Fletchery status", "body": text, "labels": [f"{graph.PREFIX}-status"]})
        else:
            self.api(f"repos/{self.repo}/issues/{n}", "PATCH", {"body": text})

    # ---- pull requests ----
    def open_prs(self) -> list[PullRequest]:
        items = self.api(f"repos/{self.repo}/pulls?state=open&per_page=100", paginate=True) or []
        out = []
        for p in items:
            labels = [l["name"] for l in p["labels"]]
            if graph.PREFIX not in labels:
                continue
            out.append(PullRequest(p["number"], p["title"], p["head"]["ref"], p["base"]["ref"], p["head"]["sha"],
                                   labels, p.get("body") or "", p.get("mergeable")))
        return out

    def labelled_prs(self, label: str, base: str) -> list[PullRequest]:
        """Every open PR into `base` with `label`, whoever opened it (merge rounds take threads' PRs too)."""
        items = self.api(f"repos/{self.repo}/pulls?state=open&base={base}&per_page=100", paginate=True) or []
        out = []
        for p in items:
            labels = [l["name"] for l in p["labels"]]
            if label.lower() not in [l.lower() for l in labels] or p.get("draft"):
                continue
            out.append(PullRequest(p["number"], p["title"], p["head"]["ref"], p["base"]["ref"], p["head"]["sha"],
                                   labels, p.get("body") or "", p.get("mergeable")))
        return out

    def pr_files(self, number: int) -> list[str]:
        items = self.api(f"repos/{self.repo}/pulls/{number}/files?per_page=100", paginate=True) or []
        return [f["filename"] for f in items]

    def create_pr(self, head: str, base: str, title: str, body: str, labels: list[str], assignee: str | None) -> int:
        p = self.api(f"repos/{self.repo}/pulls", "POST", {"title": title, "head": head, "base": base, "body": body})
        patch: dict = {"labels": labels}
        if assignee:
            patch["assignees"] = [assignee]
        self.api(f"repos/{self.repo}/issues/{p['number']}", "PATCH", patch)
        return p["number"]

    def feedback_since(self, pr: PullRequest, since: str | None) -> list[Feedback]:
        """Lane/controller comments, failed check runs and conflicts newer than `since` (ISO time)."""
        out: list[Feedback] = []
        comments = self.api(f"repos/{self.repo}/issues/{pr.number}/comments?per_page=100", paginate=True) or []
        for c in comments:
            if since and c["created_at"] <= since:
                continue
            body = c.get("body") or ""
            if _is_lane_feedback(body):
                out.append(Feedback(pr.number, "comment", body, c["created_at"]))
        runs = self.api(f"repos/{self.repo}/commits/{pr.head_sha}/check-runs?per_page=100") or {}
        for r in runs.get("check_runs", []):
            if _is_merge_fixer_run(r.get("name") or ""):
                continue
            if r.get("conclusion") in ("failure", "timed_out") and (not since or (r.get("completed_at") or "") > since):
                out.append(Feedback(pr.number, "check", f"check `{r['name']}` {r['conclusion']}: {r.get('html_url', '')}",
                                    r.get("completed_at") or ""))
        # The local runner reports to GitHub as commit statuses, not check runs ("Failed on the build PC (run N)").
        st = self.api(f"repos/{self.repo}/commits/{pr.head_sha}/status") or {}
        latest: dict[str, dict] = {}
        for s in st.get("statuses", []):  # newest first: the first one per context is the current verdict
            latest.setdefault(s.get("context", ""), s)
        for ctx, s in latest.items():
            if s.get("state") in ("failure", "error") and (not since or (s.get("updated_at") or "") > since):
                out.append(Feedback(pr.number, "status", f"`{ctx}` {s['state']}: {s.get('description') or ''} "
                                                          f"{s.get('target_url') or ''}".strip(), s.get("updated_at") or ""))
        if pr.mergeable is False:
            out.append(Feedback(pr.number, "conflict", f"PR #{pr.number} conflicts with {pr.base}", _now()))
        return out


_LANE_MARKERS = ("taken out", "lost `ready`", "failed", "conflict", "Build Lane", "staging controller", "FAIL ")


_MERGE_FIXER_JOB = re.compile(r"^(fix \(\d+,|find$)")


def _is_merge_fixer_run(name: str) -> bool:
    """Jobs of the project's "Claude merge fixer" workflow ("find", "fix (855, branch, title)"). That workflow fires on
    any PR event, so its jobs land on whichever PR's head triggered it, and they fix OTHER PRs. A failed one is not a
    verdict on this PR: LifeShare #829 was sent back twice and stopped on fixer jobs for #853 and #855 (2026-11-03)."""
    return bool(_MERGE_FIXER_JOB.match(name.strip()))


def _is_lane_feedback(body: str) -> bool:
    if "Generated by [Claude Code]" in body and "merge fixer" in body:
        return False  # the merge fixer's own report, not a verdict
    if body.lstrip().startswith(("Foreman", "Fletchery")):
        return False  # ours
    return any(m in body for m in _LANE_MARKERS)


def _is_foreman(body: str) -> bool:
    """Fletchery's own comments (and Foreman's, its name in LifeShare before it moved here)."""
    b = body.lstrip()
    return b.startswith(("Foreman", "Fletchery")) or "_Generated by Foreman" in body or "_Generated by Fletchery" in body


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeGitHub:
    """Same surface as GitHub, in memory. `writes` records every mutation so tests and dry runs can show them."""

    def __init__(self, tasks: dict[int, Task] | None = None, closed: set[int] | None = None):
        self.tasks: dict[int, Task] = tasks or {}
        self.closed: set[int] = set(closed or ())
        self.prs: list[PullRequest] = []
        self.comments: dict[int, list[str]] = {}
        self.feedback: dict[int, list[Feedback]] = {}
        self.notes: dict[int, list[tuple[str, str, str]]] = {}  # comments people left (author, at, body)
        self.status_text = ""
        self.writes: list[str] = []
        self._next_pr = 9000
        self._next_issue = max([0, *self.tasks.keys(), *self.closed]) + 1

    def open_tasks(self):
        return {n: t for n, t in self.tasks.items() if not t.closed}

    def issue_closed(self, number):
        return number in self.closed or (number in self.tasks and self.tasks[number].closed)

    def set_status(self, number, status):
        t = self.tasks[number]
        t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(status)]
        self.writes.append(f"label #{number} {graph.label_for(status)}")

    def comment(self, number, text):
        self.comments.setdefault(number, []).append(text)
        self.writes.append(f"comment #{number}: {text.splitlines()[0][:80]}")

    def issue_notes(self, number):
        return list(self.notes.get(number, []))

    def issue_thread(self, number):
        t = self.tasks[number]
        notes = [{"author": a, "at": at, "body": b, "fletchery": False} for a, at, b in self.notes.get(number, [])]
        mine = [{"author": "fletchery", "at": _now(), "body": c, "fletchery": True} for c in self.comments.get(number, [])]
        return {"number": number, "title": t.title, "body": t.body, "state": "closed" if t.closed else "open",
                "labels": list(t.labels), "url": f"https://github.com/example/repo/issues/{number}", "comments": notes + mine}

    def create_issue(self, title, body, labels):
        n = self._next_issue
        self._next_issue += 1
        self.tasks[n] = Task(n, title, body, list(labels), False, _now())
        self.writes.append(f"issue #{n} {title}")
        return n

    def edit_body(self, number, body):
        self.tasks[number].body = body
        self.writes.append(f"body #{number}")

    def marked_issues(self, marker):
        return [{"number": t.number, "body": t.body} for t in self.tasks.values() if marker in t.body]

    def status_issue(self):
        return 1

    def write_status(self, text):
        self.status_text = text
        self.writes.append("status issue rewritten")

    def open_prs(self):
        return list(self.prs)

    def labelled_prs(self, label, base):
        return [p for p in self.prs if p.base == base and label.lower() in [l.lower() for l in p.labels]]

    def pr_files(self, number):
        return list(getattr(self, "files", {}).get(number, []))

    def create_pr(self, head, base, title, body, labels, assignee):
        n = self._next_pr
        self._next_pr += 1
        self.prs.append(PullRequest(n, title, head, base, "deadbeef", list(labels), body))
        self.writes.append(f"PR #{n} {head} -> {base}: {title}")
        return n

    def feedback_since(self, pr, since):
        return [f for f in self.feedback.get(pr.number, []) if not since or f.at > since]

    def compare_commits(self, base, head):
        return list(getattr(self, "commits", []))

    def merged_pr(self, branch):
        return getattr(self, "merged", {}).get(branch)

    def close_issue(self, number):  # also what the lane does when a PR with "Closes #n" lands
        self.writes.append(f"close #{number}")
        self.closed.add(number)
        if number in self.tasks:
            self.tasks[number].closed = True
