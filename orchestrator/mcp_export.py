#!/usr/bin/env python3
r"""mcp_export.py — share Hermes's MCP servers + GUI automation with worker nodes.

Hermes owns 25 MCP servers (filesystem, github, playwright, cua-driver GUI,
blender, kicad, cineforge, iris, ...). Worker CLI agents each keep their own
private MCP config, so by default a worker is blind to every capability the
orchestrator has. This exports Hermes's server definitions into the formats the
worker CLIs read, so both layers drive the SAME tools.

  Hermes config.yaml : mcp_servers ──┐
                                     ├──► .mcp.json      (claude, cmdc, puku)
                                     └──► opencode.json  (opencode / kilocode)

ADMIN-PRIVILEGE POLICY
  The user's rule: every app is controllable by Hermes and by worker nodes,
  EXCEPT anything requiring admin/elevated rights. Servers named in ADMIN_DENY
  are never exported to workers — a worker that cannot elevate would hang on a
  UAC prompt no one can answer. Hermes keeps them and handles them itself.

USAGE
  python mcp_export.py --list
  python mcp_export.py --write --scope project --dir D:\Projects\foo
  python mcp_export.py --write --scope user
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("HERMES_HOME", r"D:\.hermes"))
CONFIG = ROOT / "config.yaml"

# Never handed to unattended workers: needs elevation or exclusive hardware.
ADMIN_DENY = {
    "open-design",   # packaged Electron app, exclusive UI session
}

# GUI automation surfaces. Exported, but flagged: only one agent may drive the
# desktop at a time or two agents fight over the same mouse.
GUI_SERVERS = {"cua-driver", "playwright"}


def load_servers() -> dict:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return cfg.get("mcp_servers") or {}


def to_mcp_json(servers: dict, include_gui: bool = True) -> dict:
    """Standard .mcp.json shape understood by claude/cmdc/puku."""
    out = {}
    for name, spec in servers.items():
        if name in ADMIN_DENY:
            continue
        if not include_gui and name in GUI_SERVERS:
            continue
        if not isinstance(spec, dict):
            continue
        if spec.get("disabled"):
            continue
        entry = {"command": spec.get("command"), "args": spec.get("args", [])}
        if spec.get("env"):
            entry["env"] = spec["env"]
        if not entry["command"]:
            continue
        out[name] = entry
    return {"mcpServers": out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--no-gui", action="store_true")
    a = ap.parse_args()

    servers = load_servers()
    doc = to_mcp_json(servers, include_gui=not a.no_gui)

    if a.list or not a.write:
        print(f"Hermes MCP servers: {len(servers)}")
        print(f"Exportable to workers: {len(doc['mcpServers'])}")
        for n in sorted(doc["mcpServers"]):
            tag = "  [GUI]" if n in GUI_SERVERS else ""
            print(f"  + {n}{tag}")
        for n in sorted(set(servers) - set(doc["mcpServers"])):
            why = "admin/exclusive" if n in ADMIN_DENY else "disabled/no-command"
            print(f"  - {n}  (withheld: {why})")
        if not a.write:
            return

    target = Path(a.dir).resolve() / ".mcp.json"
    target.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\nwrote {target}  ({len(doc['mcpServers'])} servers)")


if __name__ == "__main__":
    main()
