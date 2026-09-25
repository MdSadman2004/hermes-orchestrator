#!/usr/bin/env python3
r"""dispatch.py — Hermes orchestrator -> CLI worker nodes (LangGraph-style).

Hermes is the central node. Local CLI agents (claude, cmdc, opencode, copilot)
are worker nodes. This module is the edge between them: it selects a node,
runs it headless with a hard timeout and tree-kill, captures the result, and
lets the worker contribute to shared memory.

TOPOLOGY (LangGraph terms, no LangGraph dependency — one less thing to break)
    STATE   : the task dict threaded through every hop
    NODES   : worker CLIs, addressed by name from nodes.json
    EDGES   : route() picks the next node from task kind + cost tier
    REDUCER : shared memory bus (append-only, vector-clocked)

COST LADDER
  Tier 0 (free: opencode) is tried FIRST for bulk//wide work. Escalate to paid
  tiers only when the cheap node fails or the task is flagged hard. This is a
  measured policy, not a preference: opencode's nemotron free model answered a
  probe in ~12s at zero cost.

SAFETY
  * Every run has a timeout and is tree-killed on expiry (Windows taskkill /T),
    because an orphaned CLI child will otherwise hold the slot forever.
  * yolo/auto-approve flags are OPT-IN per call (--yolo), never the default.
  * Only nodes with verified=true are dispatchable; unauthenticated CLIs are
    refused with the exact fix instead of hanging on a login prompt.

CLI
  python dispatch.py --list
  python dispatch.py --probe
  python dispatch.py --task "summarize X" --kind bulk
  python dispatch.py --task "..." --node claude --timeout 300 --yolo
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODES = HERE / "nodes.json"
RUNS = HERE / "runs.jsonl"
MEMORY_BUS = HERE / "memory_bus.py"
MCP_CONFIG = HERE / "worker.mcp.json"


def export_mcp(target: Path) -> None:
    """Regenerate the worker MCP config from Hermes's own config.yaml."""
    subprocess.run([sys.executable, str(HERE / "mcp_export.py"),
                    "--write", "--dir", str(target.parent)],
                   capture_output=True, timeout=60)
    src = target.parent / ".mcp.json"
    if src.exists() and src != target:
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

# task kind -> ordered node preference (cheapest capable first)
ROUTES = {
    "bulk":     ["opencode", "cmdc", "claude"],
    "scan":     ["opencode", "cmdc", "claude"],
    "summarize": ["opencode", "cmdc", "claude"],
    "code":     ["cmdc", "claude", "opencode"],
    "refactor": ["claude", "cmdc"],
    "reason":   ["claude", "cmdc"],
    "research": ["claude", "cmdc", "opencode"],
    "github":   ["copilot", "cmdc", "claude"],
    "default":  ["opencode", "cmdc", "claude"],
}


def load_nodes() -> dict:
    return json.loads(NODES.read_text(encoding="utf-8"))["nodes"]


def usable(nodes: dict) -> dict:
    return {k: v for k, v in nodes.items() if v.get("verified")}


def route(kind: str, nodes: dict) -> list:
    ok = usable(nodes)
    order = ROUTES.get(kind, ROUTES["default"])
    picked = [n for n in order if n in ok]
    # anything verified but unlisted is a valid last resort
    picked += [n for n in ok if n not in picked]
    return picked


def run_node(name: str, spec: dict, prompt: str, timeout: int = 300,
             yolo: bool = False, cwd: str | None = None,
             mcp: bool = False) -> dict:
    argv_t = spec.get("argv_yolo" if yolo else "argv") or spec["argv"]
    argv = [spec["bin"]] + [a.replace("{prompt}", prompt) for a in argv_t]

    # Item 5: give the worker the SAME MCP servers + GUI automation Hermes has.
    # --strict-mcp-config is what makes this non-interactive: without it every
    # server sits at "Pending approval (run `claude` to approve)" and a headless
    # worker simply never gets the tools. Verified live 2026-09-09.
    if mcp and spec.get("mcp_flags"):
        cfg = MCP_CONFIG
        if not cfg.exists():
            export_mcp(cfg)
        argv += [f.replace("{config}", str(cfg)) for f in spec["mcp_flags"]]

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=cwd or os.environ.get("TEMP", "."),
            stdin=subprocess.DEVNULL,  # a TUI that waits on stdin must EOF, not hang
            shell=(os.name == "nt"),
        )
    except FileNotFoundError:
        return {"node": name, "ok": False, "error": f"binary not found: {spec['bin']}",
                "elapsed": 0.0, "stdout": "", "stderr": ""}

    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        rc, timed_out = -1, True

    res = {
        "node": name, "model": spec.get("model_hint", name), "cost": spec.get("cost"),
        "ok": (rc == 0 and not timed_out), "rc": rc, "timed_out": timed_out,
        "elapsed": round(time.time() - started, 1),
        "stdout": (out or "").strip(), "stderr": (err or "").strip()[:2000],
        "ts": datetime.datetime.now().astimezone().isoformat(),
    }
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in res.items() if k != "stdout"} |
                            {"stdout_len": len(res["stdout"])}) + "\n")
    return res


def dispatch(prompt: str, kind: str = "default", node: str = "",
             timeout: int = 300, yolo: bool = False, cwd: str = "",
             fallback: bool = True, mcp: bool = False) -> dict:
    nodes = load_nodes()
    if node:
        if node not in nodes:
            return {"ok": False, "error": f"unknown node '{node}'"}
        if not nodes[node].get("verified"):
            return {"ok": False, "error": f"node '{node}' not usable: {nodes[node].get('notes')}"}
        order = [node]
    else:
        order = route(kind, nodes)

    if not order:
        return {"ok": False, "error": "no verified worker nodes available"}

    attempts = []
    for n in order:
        r = run_node(n, nodes[n], prompt, timeout, yolo, cwd or None, mcp)
        attempts.append({k: r[k] for k in ("node", "ok", "rc", "elapsed", "timed_out")})
        if r["ok"] and r["stdout"]:
            r["attempts"] = attempts
            return r
        if not fallback:
            r["attempts"] = attempts
            return r
    return {"ok": False, "error": "all nodes failed", "attempts": attempts}


def probe() -> dict:
    """Live PONG probe. Rewrites verified/verified_at in nodes.json from REALITY."""
    doc = json.loads(NODES.read_text(encoding="utf-8"))
    for name, spec in doc["nodes"].items():
        r = run_node(name, spec, "Reply with exactly: PONG", timeout=120)
        ok = r["ok"] and "PONG" in r["stdout"].upper()
        spec["verified"] = bool(ok)
        spec["verified_at"] = datetime.datetime.now().strftime("%Y-%m-%d")
        if ok:
            spec["latency_s"] = int(r["elapsed"])
        print(f"{name:10s} {'OK ' if ok else 'FAIL'} {r['elapsed']:6.1f}s  "
              f"{(r['stdout'] or r['stderr'])[:70]!r}")
    doc["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d")
    NODES.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="")
    ap.add_argument("--kind", default="default")
    ap.add_argument("--node", default="")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--cwd", default="")
    ap.add_argument("--yolo", action="store_true")
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--mcp", action="store_true",
                    help="give the worker Hermes's MCP servers + GUI automation")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.list:
        for n, s in load_nodes().items():
            flag = "USABLE " if s.get("verified") else "disabled"
            print(f"{flag} {n:10s} tier={s.get('tier')} cost={s.get('cost'):10s} "
                  f"{','.join(s.get('strengths', []))}")
            if not s.get("verified"):
                print(f"          -> {s.get('notes')}")
        return

    if a.probe:
        probe()
        return

    if not a.task:
        ap.print_help()
        return

    r = dispatch(a.task, a.kind, a.node, a.timeout, a.yolo, a.cwd,
                 fallback=not a.no_fallback, mcp=a.mcp)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        if r.get("ok"):
            print(f"--- {r['node']} ({r['elapsed']}s, {r['cost']}) ---")
            print(r["stdout"])
        else:
            print(f"FAILED: {r.get('error')}")
            for at in r.get("attempts", []):
                print("  ", at)
            sys.exit(1)


if __name__ == "__main__":
    main()
