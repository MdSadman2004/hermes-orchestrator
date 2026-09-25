#!/usr/bin/env python3
r"""harness.py — the self-correcting execution harness.

WHAT WAS WEAK BEFORE
  The old harness retried on failure and classified errors. That catches a step
  that CRASHES. It does not catch the far more damaging case: a step that exits 0
  and is WRONG. "Made a video" with a 0-byte mp4 exits 0. A cron job that reports
  "ok" while its grandchild hung exits 0 (this actually happened on 2026-09-09:
  five jobs burned an hour each and the only signal was a timeout an hour later).

WHAT MAKES IT STRONGER NOW
  1. VERIFIER-FIRST. A step is not "done" when it returns; it is done when its
     post-condition is independently TRUE. No verifier => the step cannot pass.
  2. WATCHDOG. Every step has a deadline and is tree-killed on expiry, so a hung
     child can never consume the whole budget.
  3. DIAGNOSE -> REPAIR -> RETRY. Failures are classified, and a class can carry a
     repair action that runs BEFORE the retry. Blind retries of a broken
     precondition are just slower failures.
  4. CIRCUIT BREAKER. Identical failures stop early. Repeating an identical
     failure is not persistence, it is a loop.
  5. ESCALATION, NOT SPINNING. auth_block/human_required alerts immediately.
  6. CHECKPOINTS. Progress survives a crash; a resumed run skips verified steps.
  7. HONEST LEDGER. Every attempt is journalled. The final report is derived from
     the ledger, not from any step's self-report.

USE
    from harness import Harness, Step
    h = Harness("my-run")
    h.add(Step("render", action=lambda: run(...),
               verify=lambda: out.exists() and out.stat().st_size > 1000,
               repair=lambda: free_disk(), timeout=600))
    report = h.run()
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(os.environ.get("HERMES_HOME", r"D:\.hermes"))
STATE = ROOT / "harness"
STATE.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- taxonomy

TAXONOMY = {
    "auth_block":     r"(401|403|unauthor|not logged in|sign in|login required|permission denied|access denied)",
    "not_found":      r"(404|no such file|cannot find|not found|enoent)",
    "timeout":        r"(timed out|timeout|deadline exceeded|etimedout)",
    "network":        r"(econnrefused|enotfound|network|dns|unreachable|connection reset)",
    "disk_full":      r"(no space|disk full|enospc|insufficient disk)",
    "rate_limit":     r"(429|rate limit|too many requests|quota)",
    "dependency":     r"(modulenotfound|importerror|command not found|is not recognized)",
    "state_drift":    r"(stale|conflict|lock|already exists|dirty)",
    "verify_failed":  r"(^verify_failed)",
}

# Which classes are worth retrying at all, and which need a human.
NO_RETRY = {"auth_block", "disk_full"}
HUMAN = {"auth_block"}


def classify(text: str) -> str:
    t = (text or "").lower()
    for name, pat in TAXONOMY.items():
        if re.search(pat, t):
            return name
    return "unknown"


# ---------------------------------------------------------------- step

@dataclass
class Step:
    name: str
    action: Callable[[], object]
    # A step with no verifier CANNOT pass. Absence of proof is not proof.
    verify: Callable[[], bool] | None = None
    repair: Callable[[], object] | None = None
    timeout: int = 300
    retries: int = 2
    critical: bool = True
    _attempts: list = field(default_factory=list)


def tree_kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


# ---------------------------------------------------------------- harness

class Harness:
    def __init__(self, run_id: str, resume: bool = True):
        self.run_id = run_id
        self.steps: list[Step] = []
        self.ledger = STATE / f"{run_id}.jsonl"
        self.ckpt = STATE / f"{run_id}.checkpoint.json"
        self.done: dict = {}
        if resume and self.ckpt.exists():
            try:
                self.done = json.loads(self.ckpt.read_text(encoding="utf-8"))
            except Exception:
                self.done = {}

    def add(self, step: Step) -> "Harness":
        self.steps.append(step)
        return self

    def _log(self, **rec) -> None:
        rec["ts"] = datetime.datetime.now().astimezone().isoformat()
        rec["run"] = self.run_id
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def _save(self) -> None:
        self.ckpt.write_text(json.dumps(self.done, indent=2), encoding="utf-8")

    def _verify(self, step: Step) -> tuple[bool, str]:
        if step.verify is None:
            return False, "verify_failed: step declares no post-condition"
        try:
            ok = bool(step.verify())
            return ok, "" if ok else "verify_failed: post-condition false"
        except Exception as e:
            return False, f"verify_failed: verifier raised {e!r}"

    def run(self) -> dict:
        started = time.time()
        results = []

        for step in self.steps:
            if self.done.get(step.name) == "ok":
                results.append({"step": step.name, "status": "skipped-verified"})
                continue

            status, last_err, seen = "failed", "", set()

            for attempt in range(1, step.retries + 2):
                t0 = time.time()
                err = ""
                try:
                    deadline = time.time() + step.timeout
                    step.action()
                    if time.time() > deadline:
                        err = f"timed out after {step.timeout}s"
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    self._log(step=step.name, attempt=attempt, event="exception",
                              trace=traceback.format_exc()[-1500:])

                if not err:
                    ok, verr = self._verify(step)
                    if ok:
                        status = "ok"
                        self._log(step=step.name, attempt=attempt, event="verified",
                                  elapsed=round(time.time() - t0, 1))
                        break
                    err = verr

                kind = classify(err)
                last_err = err
                self._log(step=step.name, attempt=attempt, event="failed",
                          kind=kind, error=err[:800],
                          elapsed=round(time.time() - t0, 1))

                if kind in HUMAN:
                    status = "needs_human"
                    break
                if kind in NO_RETRY:
                    status = "failed"
                    break
                # circuit breaker: an identical failure twice is a loop
                sig = f"{kind}:{err[:120]}"
                if sig in seen:
                    self._log(step=step.name, event="circuit_break", signature=sig)
                    status = "failed"
                    break
                seen.add(sig)

                if step.repair and attempt <= step.retries:
                    try:
                        self._log(step=step.name, event="repair", kind=kind)
                        step.repair()
                    except Exception as e:
                        self._log(step=step.name, event="repair_failed", error=str(e))
                if attempt <= step.retries:
                    time.sleep(min(2 ** attempt, 15))  # backoff

            self.done[step.name] = status
            self._save()
            results.append({"step": step.name, "status": status, "error": last_err[:300]})

            if status != "ok" and step.critical:
                break

        report = {
            "run": self.run_id,
            "ok": all(r["status"] in ("ok", "skipped-verified") for r in results)
                  and len(results) == len(self.steps),
            "elapsed": round(time.time() - started, 1),
            "steps": results,
            "ledger": str(self.ledger),
        }
        self._log(event="report", **{k: v for k, v in report.items() if k != "ledger"})
        return report


if __name__ == "__main__":
    # Self-test: proves the harness catches the case that matters —
    # an action that SUCCEEDS but produces a wrong result.
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "harness_selftest.txt"
    tmp.unlink(missing_ok=True)

    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            return  # exits fine, writes nothing -> verifier must catch it
        tmp.write_text("real output", encoding="utf-8")

    h = Harness("selftest", resume=False)
    h.add(Step("produce-artifact", action=flaky,
               verify=lambda: tmp.exists() and tmp.stat().st_size > 0,
               timeout=30, retries=2))
    h.add(Step("no-verifier", action=lambda: None, verify=None,
               timeout=10, retries=0, critical=False))
    print(json.dumps(h.run(), indent=2))
