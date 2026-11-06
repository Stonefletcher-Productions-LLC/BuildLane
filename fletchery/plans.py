"""Durable operator requests. Future merge windows never reserve worker capacity.

Package requests use the project's existing staging release controller. Its CAS,
full-test, shared-resource and artifact proofs remain authoritative.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid
import threading


TERMINAL = {"complete", "cancelled", "blocked"}


def utc(value=""):
    if not value:
        return datetime.now(timezone.utc)
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose a valid date and time.") from exc
    if result.tzinfo is None:
        raise ValueError("The planned time must include a time zone.")
    return result.astimezone(timezone.utc)


class Plans:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.active_merge = None
        # A lost coroutine cannot be silently restarted against a mutable candidate.
        for row in self.rows:
            if row["kind"] == "merge" and row["state"] in ("running", "draining"):
                last = engine.state.merge_last
                if last.get("request_id") == row["id"]:
                    self.finish_merge(row["id"], last)
                else:
                    self.update(row, state="blocked", reason="Fletchery restarted during this window. Review saved merge recovery before retrying.")

    @property
    def rows(self):
        return self.engine.state.integration_queue

    def save(self):
        if not self.engine.dry_run:
            self.engine.state.save(self.engine.state_dir / "state.json")

    def update(self, row, **values):
        row.update(values)
        self.save()

    def view(self):
        return {"planned": self.engine.state.planned_integration, "requests": deepcopy(self.rows),
                "base": self.engine.cfg.base_branch}

    def enqueue(self, data):
        kind = data.get("kind")
        if kind not in ("merge", "package"):
            raise ValueError("Choose a merge window or package.")
        when = utc(data.get("not_before", "")).isoformat(timespec="seconds")
        row = {"id": str(uuid.uuid4()), "kind": kind, "not_before": when,
               "created": utc().isoformat(timespec="seconds"), "state": "queued", "reason": "",
               "base": self.engine.cfg.base_branch}
        if kind == "merge":
            if not self.engine.cfg.merge_rounds:
                raise ValueError("Enable merge rounds before queuing a window.")
            if self.engine.state.merge_resume:
                raise ValueError("Resolve the saved merge round before queuing a new window.")
            import rounds
            ready = rounds.ready_prs(self.engine.ctx.gh.labelled_prs(self.engine.cfg.merge_label or "ready", row["base"]), self.engine.cfg)
            numbers = data.get("prs", [])
            if not isinstance(numbers, list) or any(type(n) is not int or n <= 0 for n in numbers):
                raise ValueError("PRs must be a list of positive numbers.")
            chosen = [p for p in ready if not numbers or p.number in numbers]
            if numbers and {p.number for p in chosen} != set(numbers):
                raise ValueError("Every selected PR must still be ready and target this project's base branch.")
            if not chosen or len(chosen) > self.engine.cfg.merge_round_max:
                raise ValueError(f"Choose between 1 and {self.engine.cfg.merge_round_max} ready PRs.")
            row["heads"] = {str(p.number): p.head_sha for p in chosen}
            row["titles"] = {str(p.number): p.title for p in chosen}
        else:
            version = str(data.get("version", "")).strip()
            if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version) or len(version) > 64:
                raise ValueError("Enter a version such as 0.3.7.")
            target, dependency = str(data.get("target", "")).strip(), str(data.get("after", ""))
            if bool(target) == bool(dependency):
                raise ValueError("Choose either an exact source revision or a queued merge window.")
            if target and not re.fullmatch(r"[a-f0-9]{40}", target):
                raise ValueError("The source revision must be a full 40-character commit SHA.")
            if dependency and not any(r["id"] == dependency and r["kind"] == "merge" and r["state"] != "cancelled" for r in self.rows):
                raise ValueError("Choose an existing merge window.")
            row.update(version=version, target=target, after=dependency)
        if len([r for r in self.rows if r["state"] not in TERMINAL]) >= 100:
            raise ValueError("The queue already has 100 pending requests.")
        self.rows.append(row)
        # Queuing is explicit authorization to use planned mode; a separate hold
        # remains authoritative and is never silently cleared by this operation.
        self.engine.state.planned_integration = True
        self.save()
        self.engine.wake()
        return deepcopy(row)

    def cancel(self, request_id):
        with self.lock:
            return self._cancel(request_id)

    def _cancel(self, request_id):
        row = next((r for r in self.rows if r["id"] == request_id), None)
        if row is None:
            raise ValueError("Queue request not found.")
        if row["state"] not in ("queued", "draining", "blocked") or row.get("dispatch_started"):
            raise ValueError("This request has started. Use its merge or build controls; running work is preserved.")
        self.update(row, state="cancelled", reason="Cancelled before execution.")
        self.engine.wake()
        return {"message": "Request cancelled; feature work continues."}

    def due(self, kind):
        return sorted((r for r in self.rows if r["kind"] == kind and r["state"] not in TERMINAL
                       and utc(r["not_before"]) <= utc()), key=lambda r: (r["not_before"], r["created"]))

    def merge_gate(self, draining):
        """Return whether the legacy merge scheduler may run, before any reservation."""
        self.active_merge = None
        engine = self.engine
        if not engine.state.planned_integration:
            return True
        if engine.round_job is not None:
            return True
        if engine.state.merge_resume.get("authorized"):
            return True  # explicit saved-candidate recovery uses the existing guards
        due = self.due("merge")
        package_busy = any(r["kind"] == "package" and r.get("dispatch_started") and r["state"] not in TERMINAL for r in self.rows)
        if package_busy:
            engine.merge_why = "A queued package owns the integration window; feature work continues"
            return False
        for package in self.due("package"):
            parent = next((r for r in self.rows if r["id"] == package.get("after")), None)
            if package.get("target") or (parent and parent["state"] == "complete"):
                engine.merge_why = "A due package is being submitted before the next merge; feature work continues"
                return False
        if not due:
            engine.merge_why = "Feature work: no merge window is due"
            return False
        row = due[0]
        if draining or engine.state.held or engine.state.merge_held:
            self.update(row, state="queued", reason="Merge window is due; waiting for merging to be resumed.")
            return False
        if engine.state.merge_resume:
            self.update(row, state="blocked", reason="A saved merge needs recovery. Candidate and feature work are preserved.")
            return False
        if row["base"] != engine.cfg.base_branch:
            self.update(row, state="blocked", reason="The project's base branch changed. Queue a new preview.")
            return False
        self.active_merge = row
        # Drain only after this exact candidate is verified eligible in _merge_round.
        return True

    def select(self, ready):
        row = self.active_merge
        if row is None:
            return ready
        if row["state"] == "cancelled":
            return []
        chosen = [p for p in ready if str(p.number) in row["heads"]]
        if {str(p.number): p.head_sha for p in chosen} != row["heads"]:
            self.update(row, state="blocked", reason="Selected PR heads or eligibility changed. Review and queue a new preview.")
            self.engine.merge_why = row["reason"]
            return []
        return chosen

    def drain(self):
        row = self.active_merge
        if row is not None and row["state"] == "cancelled":
            self.engine.merge_priority = False
            self.engine.merge_why = "Merge window cancelled; feature work continues"
            return True
        if row is None or not self.engine.jobs:
            return False
        self.update(row, state="draining", reason="Finishing active sessions before the merge window.")
        self.engine.merge_why = row["reason"]
        self.engine.merge_priority = True
        return True

    def started(self, n):
        with self.lock:
            return self._started(n)

    def _started(self, n):
        if self.active_merge is None and self.engine.state.merge_resume.get("authorized"):
            self.active_merge = next((r for r in self.rows if r["kind"] == "merge" and r.get("round") == n
                                      and r["state"] != "cancelled"), None)
        if self.active_merge:
            if self.active_merge["state"] == "cancelled":
                raise ValueError("The queued window was cancelled before execution.")
            self.update(self.active_merge, state="running", round=n, reason="Merge window running.")
            return self.active_merge["id"]
        return ""

    def finish_merge(self, request_id, result):
        row = next((r for r in self.rows if r["id"] == request_id), None)
        if row and result.get("result") in ("limited", "unreachable"):
            # The agent stopped, not the PRs: the same window runs again on the next available agent.
            self.update(row, state="queued", result=deepcopy(result),
                        reason=f"The round's agent stopped ({result.get('result')}); it runs again on another agent.")
            return
        if row:
            landed = result.get("result") == "landed"
            ok = landed and {str(n) for n in result.get("merged", [])} == set(row["heads"]) and not result.get("left_out") and not result.get("deferred")
            self.update(row, state="complete" if ok else "blocked", result=deepcopy(result),
                        reason=("Merge landed." if ok else "The round did not report all selected PRs landed; dependent packages wait." if landed
                                else "Merge did not land; review saved recovery before queuing more integration."))

    def package_step(self):
        """Run off the event loop. Persist before dispatch; never resend uncertainty."""
        engine = self.engine
        if engine.dry_run or engine.state.held or engine.round_job is not None or engine._reload_draining:
            return
        candidates = self.due("package")
        if not candidates:
            return
        row = candidates[0]
        try:
            if row.get("after") and not row.get("target"):
                parent = next(r for r in self.rows if r["id"] == row["after"])
                if parent["state"] in ("blocked", "cancelled"):
                    self.update(row, reason="Waiting for the selected merge window to complete successfully.")
                    return
                if parent["state"] != "complete":
                    return
                pr = parent.get("result", {}).get("pr")
                if not pr:
                    raise ValueError("The merge result has no landing PR; exact package source cannot be verified.")
                landed = engine.ctx.gh.api(f"repos/{engine.cfg.repo}/pulls/{int(pr)}")
                if not landed.get("merged") or landed.get("base", {}).get("ref") != row["base"]:
                    raise ValueError("The merge window's landing PR has not merged into the expected base branch.")
                target = landed.get("merge_commit_sha", "")
                if not re.fullmatch(r"[a-f0-9]{40}", target):
                    raise ValueError("Cannot verify the landed source revision.")
                self.update(row, target=target)
            gh, repo = engine.ctx.gh, engine.cfg.repo
            ref = gh.api(f"repos/{repo}/git/ref/heads/lane/staging-state")["object"]["sha"]
            file = gh.api(f"repos/{repo}/contents/state.json?ref={ref}")
            if file.get("encoding") != "base64" or file.get("size", 0) > 1024 * 1024:
                raise ValueError("The release controller state cannot be verified.")
            state = json.loads(base64.b64decode(file["content"]))
            if state.get("schemaVersion") != 1:
                raise ValueError("Unsupported release controller state.")
            receipt = next((r for r in state.get("requests", []) if r.get("requestId") == row["id"]), None)
            response = next((r for r in state.get("controllerRequests", []) if r.get("requestId") == row["id"]), None)
            if receipt:
                if receipt.get("target") != row["target"] or receipt.get("version") != row["version"]:
                    raise ValueError("Release receipt does not match this request's source and version.")
                package = receipt.get("package") or {}
                verified = (receipt.get("status") == "verified" and receipt.get("testSet") == "Full"
                            and package.get("result") == "success" and package.get("artifactVerified") is True
                            and package.get("source") == receipt.get("output") and package.get("version") == row["version"])
                if receipt.get("status") == "verified" and not verified:
                    raise ValueError("The release receipt lacks matching full-test and verified package proofs.")
                failed = receipt.get("status") in ("failed", "cancelled", "rejected")
                self.update(row, state="complete" if verified else "blocked" if failed else "running",
                            reason="Full tests and package verified." if verified else f"Release controller: {receipt.get('status', 'pending')}",
                            receipt=deepcopy(receipt), output=package.get("path", ""))
                return
            if response and response.get("status") in ("failed", "rejected"):
                raise ValueError(str(response.get("message") or "Release controller rejected the request."))
            if row.get("dispatch_started"):
                self.update(row, state="submitted", reason="Awaiting the controller's durable result; not resending this request.")
                return
            if state.get("work") or state.get("integrationPaused"):
                self.update(row, reason="Waiting for the release controller's current work or pause to finish.")
                return
            if state.get("integrationMode") != "MergeToStagingThenValidate":
                raise ValueError("Package queuing requires the project's staging release controller.")
            with self.lock:
                if row["state"] != "queued":
                    return
                self.update(row, state="submitted", dispatch_started=utc().isoformat(), reason="Submitting full tests and package to the release controller.")
            # Existing controller enforces eligibility and the expected state revision.
            gh.api(f"repos/{repo}/actions/workflows/staging-controller.yml/dispatches", "POST", {
                "ref": "main", "inputs": {"operation": "release", "request_id": row["id"],
                "expected_revision": ref, "details": json.dumps({"target": row["target"], "version": row["version"]})}})
        except Exception as exc:
            if row["state"] == "cancelled":
                return
            uncertain = row.get("dispatch_started") and not isinstance(exc, ValueError)
            self.update(row, state="submitted" if uncertain else "blocked",
                        reason=("Dispatch is unconfirmed; checking the durable result without resending. " if row.get("dispatch_started") else "") + str(exc)[:500])
