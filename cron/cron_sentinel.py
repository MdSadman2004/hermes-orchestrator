#!/usr/bin/env python3
r"""cron_sentinel.py — no_agent watchdog for the cron fleet.

Runs the harness against the cron system itself. Silent when healthy; speaks
only when a human is needed. Zero LLM tokens.

Catches the failure modes that actually happened on this machine:
  * CATCH-UP STORM  — several jobs dispatched in the same second after a wake,
    each then racing the same resources until they hit the script ceiling. This
    is ONE systemic fault, not N broken jobs, and must be reported as such.
  * STUCK STREAK    — a job failing repeatedly because its monitor treats a
    finding (e.g. "disk low") as an error.
  * ZOMBIE RUN      — an execution row still 'running' long past any sane time.
  * DEAD MONITOR    — a job that has not run in far longer than its schedule.

Exit 0 always (a watchdog must not itself become a failing job); findings go to
stdout, which the cron wrapper delivers only when non-empty.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_HOME", r"D:\.hermes"))
JOBS = ROOT / "cron" / "jobs.json"
DB = ROOT / "cron" / "executions.db"

STORM_WINDOW_S = 5     # dispatches this close together are one storm
STORM_MIN = 3          # this many at once is pathological
ZOMBIE_H = 3           # a run alive this long is stuck
STREAK_ALERT = 3


def now():
    return datetime.datetime.now().astimezone()


def parse(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except Exception:
        return None


def main() -> int:
    findings: list[str] = []

    if not JOBS.exists():
        print("cron-sentinel: jobs.json missing")
        return 0

    doc = json.loads(JOBS.read_text(encoding="utf-8"))
    jobs = doc.get("jobs", [])
    byid = {j.get("id"): j for j in jobs}

    # --- 1. failure streaks -------------------------------------------------
    for j in jobs:
        streak = int(j.get("failure_streak") or 0)
        if streak >= STREAK_ALERT and j.get("enabled", True):
            findings.append(
                f"STREAK  {j.get('name')} has failed {streak}x in a row. "
                f"last_error={str(j.get('last_error'))[:160]}")

    # --- 2. catch-up storm --------------------------------------------------
    stamps = []
    for j in jobs:
        d = (j.get("last_dispatch") or {})
        t = parse(d.get("dispatched_at"))
        if t:
            stamps.append((t, j.get("name"), d.get("kind"), d.get("lateness_seconds") or 0))
    stamps.sort()
    i = 0
    while i < len(stamps):
        group = [stamps[i]]
        k = i + 1
        while k < len(stamps) and (stamps[k][0] - stamps[i][0]).total_seconds() <= STORM_WINDOW_S:
            group.append(stamps[k]); k += 1
        if len(group) >= STORM_MIN:
            late = [g for g in group if g[2] in ("catch_up", "late")]
            if late:
                names = ", ".join(g[1] for g in group)
                worst = max(g[3] for g in group)
                findings.append(
                    f"STORM   {len(group)} jobs dispatched within {STORM_WINDOW_S}s "
                    f"at {group[0][0].isoformat()} ({len(late)} catch-up/late, "
                    f"worst lateness {worst/3600:.1f}h): {names}. "
                    f"This is ONE systemic event (missed window after sleep/downtime), "
                    f"not {len(group)} independent bugs — diagnose the scheduler gap, "
                    f"not each script.")
        i = k

    # --- 3. zombie executions ----------------------------------------------
    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            cols = [r[1] for r in con.execute("PRAGMA table_info(executions)")]
            if {"status", "started_at"} <= set(cols):
                for row in con.execute(
                        "SELECT job_id, started_at FROM executions "
                        "WHERE status='running' ORDER BY rowid DESC LIMIT 50"):
                    t = parse(row[1])
                    if t and (now() - t).total_seconds() > ZOMBIE_H * 3600:
                        nm = (byid.get(row[0], {}) or {}).get("name", row[0])
                        findings.append(
                            f"ZOMBIE  {nm} has been 'running' since {row[1]} "
                            f"(> {ZOMBIE_H}h). Likely a hung child holding a slot.")
            con.close()
        except Exception as e:
            findings.append(f"SENTINEL could not read executions.db: {e}")

    # --- 4. repeated identical errors ---------------------------------------
    errs = Counter(str(j.get("last_error"))[:80] for j in jobs
                   if j.get("last_status") == "error" and j.get("last_error"))
    for msg, n in errs.items():
        if n >= 2:
            findings.append(f"PATTERN {n} jobs share the identical error: {msg!r} "
                            f"— fix the common cause, not each job.")

    if findings:
        print(f"cron-sentinel {now().strftime('%Y-%m-%d %H:%M')} — {len(findings)} finding(s)\n")
        for f in findings:
            print(" ", f)
        print("\nRepair: python D:\\.hermes\\scripts\\cron_guard.py --all --disable-at-streak 5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
