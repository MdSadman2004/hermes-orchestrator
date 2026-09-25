#!/usr/bin/env python3
r"""prompt_router.py — stage 2 of  prompt -> PROMPT LIBRARY -> work structure.

A raw user prompt is matched against the prompt library, and what comes back is
not a "better prompt" but a WORK STRUCTURE: ordered steps, the skills to load,
the worker-node route, the verification post-condition, and the invariants that
apply to every job (never write to C:, no overstated claims, etc).

  raw prompt ──► match(triggers) ──► entry ──► expand() ──► work structure
                                                  │
                                                  └─► invariants always injected

Matching is deliberately dumb and deterministic (trigger substrings + score), so
the routing decision is auditable and costs zero tokens. Hermes stays free to
override; this is a floor, not a cage.

CLI
  python prompt_router.py "make a reel from the drone footage"
  python prompt_router.py --list
  python prompt_router.py --json "benchmark the ECG model"
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "prompt_library.json"


def load() -> dict:
    return json.loads(LIB.read_text(encoding="utf-8"))


def match(prompt: str, lib: dict) -> list:
    """Score every entry by trigger hits. Longer triggers score higher (more specific)."""
    p = prompt.lower()
    scored = []
    for name, e in lib["entries"].items():
        score, hits = 0, []
        for t in e.get("triggers", []):
            if re.search(r"(?<!\w)" + re.escape(t.lower()), p):
                score += len(t.split())  # multi-word triggers are more specific
                hits.append(t)
        if score:
            scored.append((score, name, hits))
    return sorted(scored, reverse=True)


def expand(prompt: str, name: str, lib: dict) -> dict:
    e = lib["entries"][name]
    return {
        "prompt": prompt,
        "entry": name,
        "work_structure": e["work_structure"],
        "skills": e.get("skills", []),
        "route": e.get("route", "default"),
        "verify": e.get("verify", ""),
        "hard_rule": e.get("hard_rule", ""),
        "invariants": lib["invariants"],
    }


def render(spec: dict) -> str:
    L = [f"WORK STRUCTURE  ({spec['entry']})", ""]
    L.append("Steps:")
    L += [f"  {s}" for s in spec["work_structure"]]
    if spec.get("hard_rule"):
        L += ["", f"HARD RULE: {spec['hard_rule']}"]
    L += ["", f"Load skills : {', '.join(spec['skills']) or '(none)'}"]
    L += [f"Worker route: {spec['route']}"]
    L += [f"Done when   : {spec['verify']}"]
    L += ["", "Invariants (always apply):"]
    for k, v in spec["invariants"].items():
        if k.startswith("_"):
            continue
        L.append(f"  - {v}")
    return "\n".join(L)


def resolve(prompt: str) -> dict | None:
    lib = load()
    m = match(prompt, lib)
    if not m:
        return None
    return expand(prompt, m[0][1], lib) | {"matched": m[0][2], "alternates": [x[1] for x in m[1:3]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    lib = load()

    if a.list:
        for n, e in lib["entries"].items():
            print(f"{n:22s} route={e.get('route','default'):9s} "
                  f"triggers: {', '.join(e['triggers'][:5])}")
        return

    prompt = " ".join(a.prompt)
    if not prompt:
        ap.print_help()
        return

    spec = resolve(prompt)
    if not spec:
        print("No library entry matched. Falling back to general handling.\n"
              "Consider adding an entry to prompt_library.json if this recurs.")
        return
    print(json.dumps(spec, indent=2) if a.json else render(spec))
    if spec.get("alternates"):
        print(f"\n(alternates considered: {', '.join(spec['alternates'])})")


if __name__ == "__main__":
    main()
