"""GitHub access through `gh api` (REST only: the lane's helpers and this session's proxy both avoid GraphQL),
and an in-memory fake with the same surface for tests and `--dry-run`."""
from __future__ import annotations

import json
import re
import subprocess
import time
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


# GETs of one issue, its comments, one PR or a branch's closed PRs are answered from memory for a short while.
# Every pass re-read each blocked task's thread, each closed prerequisite and each PR-state task's branch, so with
# ~100 open tasks Fletchery used ~90 calls a minute, ran through the 5,000/hour REST limit and every pass then
# failed until the hour reset (LifeShare 2026-11-07 ~17:00-17:35Z: nothing started). Writes drop the entries of the
# issue/PR they touch, so Fletchery always sees its own changes at once.
_CACHEABLE = re.compile(r"^repos/[^/]+/[^/]+/(issues/\d+(/comments\?.*)?|pulls/\d+|pulls\?state=closed&head=.*)$")
# Failures before the request reached GitHub: safe to repeat even for a POST.
_NEVER_CONNECTED = ("connectex", "connection refused", "no such host", "temporary failure in name resolution",
                    "tls handshake timeout", "dial tcp")
_NUMBER = re.compile(r"^repos/[^/]+/[^/]+/(?:issues|pulls)/(\d+)")


class GitHub:
    cache_seconds = 120            # how stale a cached GET may be (a person's new comment shows up within this)
    closed_cache_seconds = 3600    # a closed issue or merged PR rarely reopens: keep that answer longer

    def __init__(self, repo: str, gh: str = "gh"):
        self.repo = repo
        self.gh = gh
        self.warnings: list[str] = []
        self._cache: dict[tuple[str, bool], tuple[float, object]] = {}

    # ---- raw ----
    def _request(self, path: str, method: str = "GET", body: dict | None = None, paginate: bool = False) -> str:
        cmd = [self.gh, "api", "-X", method, path]
        if paginate:
            cmd.append("--paginate")
        if body is not None:
            cmd += ["--input", "-"]
        # gh prints UTF-8; Windows' default (cp1252) can't decode some bytes and the whole pass failed (2026-11-03)
        # Reads retry once. A write retries only when the connection never opened (nothing reached GitHub), so a retry
        # can't post a comment twice; a dropped PATCH crashed task #1013's planner and two worker passes (2026-11-08/09).
        attempts = 2 if method == "GET" else 3
        for attempt in range(attempts):
            if attempt and method != "GET":
                time.sleep(2 * attempt)
            try:
                p = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=15 if method == "GET" else 90)
            except subprocess.TimeoutExpired as e:
                if method == "GET" and attempt + 1 < attempts:  # a write that timed out may have landed
                    continue
                raise RuntimeError(f"GitHub {method} {path} timed out; will retry on the next short dispatch pass") from e
            if p.returncode == 0:
                break
            detail = p.stderr.strip() or p.stdout.strip()
            low = detail.lower()
            if method == "GET":
                transient = any(x in low for x in ("timeout", "timed out", "connectex", "connection reset", "connection refused", "no such host", "temporary failure", "tls handshake"))
            else:
                transient = any(x in low for x in _NEVER_CONNECTED)
            if not transient or attempt + 1 == attempts:
                raise RuntimeError(f"gh api {method} {path}: {detail}")
        return p.stdout

    def api(self, path: str, method: str = "GET", body: dict | None = None, paginate: bool = False, fresh: bool = False):
        cacheable = method == "GET" and bool(_CACHEABLE.match(path))
        key = (path, paginate)
        if cacheable and not fresh:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        output = self._request(path, method, body, paginate)
        value = parse_pages(output) if paginate else (json.loads(output) if output.strip() else None)
        if cacheable:
            done = isinstance(value, dict) and (value.get("state") == "closed" or bool(value.get("merged_at")))
            self._cache[key] = (time.monotonic() + (self.closed_cache_seconds if done else self.cache_seconds), value)
            if len(self._cache) > 4000:
                now = time.monotonic()
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        elif method != "GET":
            self.forget(path)
        return value

    def forget(self, path: str) -> None:
        """Drop cached answers about the issue or PR a write touched (and branch PR lists, which a merge changes)."""
        m = _NUMBER.match(path)
        if not m:
            return
        n = m.group(1)
        marks = (f"/issues/{n}", f"/pulls/{n}")
        self._cache = {k: v for k, v in self._cache.items()
                       if not any(k[0].endswith(x) or (x + "/") in k[0] or (x + "?") in k[0] for x in marks)
                       and "pulls?state=closed" not in k[0]}

    def api_text(self, path: str) -> str:
        """A bounded GET whose answer is plain text (a job's log)."""
        return self._request(path)

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

    def issue_closed(self, number: int, repository: str | None = None) -> bool:
        repo = repository or self.repo
        it = self.api(f"repos/{repo}/issues/{number}")
        if it is None or it["state"] != "closed":
            return False
        if it.get("pull_request"):
            # An abandoned PR is closed too, but its implementation never landed.
            pr = self.api(f"repos/{repo}/pulls/{number}")
            return bool(pr and pr.get("merged_at"))
        return repository is None or it.get("state_reason") == "completed"

    def set_status(self, number: int, status: str) -> None:
        """`status` is a plain state (queued, running, pr, blocked); the issue gets "<label>:<state>"."""
        it = self.api(f"repos/{self.repo}/issues/{number}", fresh=True)  # never drop a label someone just added
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
                "comments": [{"id": c.get("id"), "author": c["user"]["login"], "at": c["created_at"], "body": c.get("body") or "",
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

    def merged_heads(self, branch: str) -> set[str]:
        """Head commits of PRs from `branch` that merged. GitHub keeps refs/pull/<n>/head, so a checkout sitting on one
        of these commits holds nothing unique even after its branch is deleted or the base history is rewritten."""
        owner = self.repo.split("/")[0]
        items = self.api(f"repos/{self.repo}/pulls?state=closed&head={owner}:{branch}&per_page=30") or []
        return {p["head"]["sha"] for p in items if p.get("merged_at") and p.get("head", {}).get("sha")}

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

    def _open_from(self, head: str, base: str) -> list[dict]:
        owner = self.repo.split("/")[0]
        return self.api(f"repos/{self.repo}/pulls?state=open&head={owner}:{head}&base={base}&per_page=10") or []

    def open_pr_for(self, head: str, base: str) -> int | None:
        """The open PR from `head` into `base`, or None."""
        items = self._open_from(head, base)
        return items[0]["number"] if items else None

    def orphan_pr_for(self, head: str, base: str) -> int | None:
        """An open PR from `head` that lacks the Fletchery label: opened by a run that then failed to label it."""
        for p in self._open_from(head, base):
            if graph.PREFIX not in [l["name"] for l in p.get("labels", [])]:
                return p["number"]
        return None

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.api(f"repos/{self.repo}/issues/{number}/labels", "POST", {"labels": labels})

    def create_pr(self, head: str, base: str, title: str, body: str, labels: list[str], assignee: str | None) -> int:
        """Open (or reuse) the PR from `head` and label it. Idempotent: a retry after a half-finished attempt finds the
        PR the first attempt opened instead of failing with 422 (2026-11-05/06: ~35 finished tasks were requeued or
        blocked that way, and their PRs sat unlabelled, so no merge round ever picked them up)."""
        number = self.open_pr_for(head, base)
        if number is None:
            try:
                number = self.api(f"repos/{self.repo}/pulls", "POST", {"title": title, "head": head, "base": base, "body": body})["number"]
            except RuntimeError:
                number = self.open_pr_for(head, base)  # an ambiguous earlier POST may have opened it after all
                if number is None:
                    raise
        else:
            self.api(f"repos/{self.repo}/pulls/{number}", "PATCH", {"title": title, "body": body})
        self.add_labels(number, labels)
        if assignee:
            # Optional. A login that cannot be assigned on this repository made the combined labels+assignees PATCH
            # fail with 422 after the PR was already open; GitHub ignores unassignable logins on this endpoint.
            try:
                self.api(f"repos/{self.repo}/issues/{number}/assignees", "POST", {"assignees": [assignee]})
            except RuntimeError as e:
                self.warnings.append(f"PR #{number}: could not assign {assignee}: {e}")
        return number

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
        self.repository_closed: set[tuple[str, int]] = set()
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

    def issue_closed(self, number, repository=None):
        if repository is not None:
            return (repository.lower(), number) in self.repository_closed
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
        mine = [{"id": f"mine-{i}", "author": "fletchery", "at": _now(), "body": c, "fletchery": True}
                for i, c in enumerate(self.comments.get(number, []))]
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

    def open_pr_for(self, head, base):
        return next((p.number for p in self.prs if p.head == head and p.base == base), None)

    def orphan_pr_for(self, head, base):
        return next((p.number for p in self.prs if p.head == head and p.base == base and graph.PREFIX not in p.labels), None)

    def add_labels(self, number, labels):
        for p in self.prs:
            if p.number == number:
                p.labels = list(dict.fromkeys(list(p.labels) + list(labels)))
        self.writes.append(f"labels #{number}: {', '.join(labels)}")

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

    def merged_heads(self, branch):
        return set(getattr(self, "merged_shas", {}).get(branch, ()))

    def close_issue(self, number):  # also what the lane does when a PR with "Closes #n" lands
        self.writes.append(f"close #{number}")
        self.closed.add(number)
        if number in self.tasks:
            self.tasks[number].closed = True
