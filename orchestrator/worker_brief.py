#!/usr/bin/env python3
r"""worker_brief.py — the context + contract handed to every worker node.

A worker CLI knows nothing about this system. Before it does real work it needs:
  * the invariants it must not violate (never C:, no overstated claims, ...),
  * what the swarm already knows (shared memory, read-only summary),
  * how to contribute back (append-only, vector-clocked),
  * its verification obligation.

This renders that brief as text to prepend to a worker prompt. Keep it SHORT —
a brief that costs more than the task defeats the point.

  python worker_brief.py --node opencode --topic Research --task "..."
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "prompt_library.json"
BUS = HERE / "memory_bus.py"


def recent_memory(topic: str = "", limit: int = 8) -> list:
    try:
        out = subprocess.run(
            [sys.executable, str(BUS), "--read", "--json", "--limit", str(limit)]
            + (["--topic", topic] if topic else []),
            capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout or "[]")
    except Exception:
        return []


def build(node: str, task: str, topic: str = "") -> str:
    lib = json.loads(LIB.read_text(encoding="utf-8"))
    inv = lib["invariants"]

    L = [
        f"You are worker node '{node}' in a Hermes-orchestrated swarm.",
        "Hermes is the central orchestrator; you are one worker. Do the task below and nothing else.",
        "",
        "NON-NEGOTIABLE RULES:",
    ]
    L += [f"  - {v}" for k, v in inv.items() if not k.startswith("_")]

    mem = recent_memory(topic)
    if mem:
        L += ["", "WHAT THE SWARM ALREADY KNOWS (do not re-derive):"]
        for m in mem[-6:]:
            L.append(f"  - [{m['topic']}] by {m['node']} at {m['ts'][:19]}: {Path(m['file']).name}")

    L += [
        "",
        "CONTRIBUTING BACK TO MEMORY:",
        "  Only if you learned something durable and NON-OBVIOUS that another agent would",
        "  otherwise have to rediscover. Do not log task progress or restate the task.",
        f"  Command: python {BUS} --append --node {node} --model <your-model> \\",
        "             --topic <Topic> --text \"<one or two sentences>\"",
        "  Memory is APPEND-ONLY and vector-clocked. Never edit or delete another node's entry.",
        "  Create a new --topic only if no existing topic fits.",
        "",
        "VERIFICATION OBLIGATION:",
        "  Do not claim success from your own narration. Show the command output, the file",
        "  that exists, or the test that passed. If you could not verify it, say so plainly.",
        "",
        "TASK:",
        f"  {task}",
    ]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="worker")
    ap.add_argument("--task", required=True)
    ap.add_argument("--topic", default="")
    a = ap.parse_args()
    print(build(a.node, a.task, a.topic))


if __name__ == "__main__":
    main()
