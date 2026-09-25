#!/usr/bin/env python3
r"""cron_guard.py — disable a cron job after N consecutive failures.

FIXED 2026-09-09: this script had NEVER worked. It read jobs.json as a dict
keyed by job id (`jobs.get(args.job)`) and read `consecutive_failures`. The real
schema is {"jobs": [ {...}, ... ], "updated_at": ...} and the streak field is
`failure_streak`. Every invocation silently printed "job not found" and exited
0, so the failure-streak circuit breaker was dead in the water.

CONTRACT
  purpose:   disable a cron job once its failure streak reaches a threshold.
             Refuses writes to C:. Never deletes a job, only disables it.
  invoked:   python D:\.hermes\scripts\cron_guard.py --disable-at-streak N (--job ID | --all) [--json]
  reads:     D:\.hermes\cron\jobs.json
  writes:    D:\.hermes\cron\jobs.json (enabled flag only) + D:\.hermes\autonomy\cron_guard.jsonl
  exit:      0 = ok | 2 = contract violation
  token_cost: 0 (no_agent)
"""
import argparse
import datetime
import json
from pathlib import Path

JOBS = Path(r"D:\.hermes\cron\jobs.json")
LOG = Path(r"D:\.hermes\autonomy\cron_guard.jsonl")


def _find(jobs, ident):
    for j in jobs:
        if j.get("id") == ident or j.get("name") == ident:
            return j
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disable-at-streak", type=int, required=True)
    ap.add_argument("--job", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="evaluate every job")
    args = ap.parse_args()

    if not JOBS.exists():
        out = {"disabled": False, "reason": "no jobs.json"}
        print(json.dumps(out) if args.json else out["reason"])
        return

    doc = json.loads(JOBS.read_text(encoding="utf-8"))
    jobs = doc.get("jobs", [])

    if args.all:
        targets = jobs
    else:
        if not args.job:
            print("--job is required unless --all is given")
            raise SystemExit(2)
        found = _find(jobs, args.job)
        targets = [found] if found else []

    if not targets:
        out = {"disabled": False, "reason": "job not found", "job": args.job}
        print(json.dumps(out) if args.json else out["reason"])
        return

    changed, results = False, []
    for j in targets:
        streak = int(j.get("failure_streak") or 0)
        did = False
        if streak >= args.disable_at_streak and j.get("enabled", True):
            j["enabled"] = False
            did = changed = True
        results.append({"job": j.get("id"), "name": j.get("name"),
                        "streak": streak, "disabled": did})

    if changed:
        JOBS.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            for r in results:
                if r["disabled"]:
                    fh.write(json.dumps(
                        {"ts": datetime.datetime.now().isoformat(), **r}) + "\n")

    if args.json:
        print(json.dumps({"results": results, "changed": changed}))
    else:
        for r in results:
            print(f"{r['name']}: streak={r['streak']} disabled={r['disabled']}")


if __name__ == "__main__":
    main()
