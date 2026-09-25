#!/usr/bin/env python3
r"""memory_bus.py — append-only shared memory for Hermes + CLI worker nodes.

WHY THIS EXISTS
  Worker nodes (claude, cmdc, opencode, copilot) run in their own processes with
  their own context. They need to read what the swarm knows and contribute what
  they learn, without ever corrupting the human-curated memory.

DESIGN RULES (from the user's spec)
  1. APPEND-ONLY. A node may add; it may never edit or delete another node's
     entry, and it may never touch D:\.hermes\memories\MEMORY.md (Hermes's own
     curated memory). Enforced structurally: writes go to a separate store.
  2. A node creates a new folder/topic only when it believes one is needed.
  3. Every append is stamped with a VECTOR CLOCK plus the wall time and the
     model/node that wrote it.
  4. Hermes can read all of it.

VECTOR CLOCKS
  A plain timestamp cannot order events across concurrent agents with skewed
  clocks. Each append carries {node: counter} for every node seen so far; the
  writer increments its own counter. Two entries are then comparable:
  before / after / concurrent. `--causality` reports concurrent writes, which is
  how you spot two nodes that independently claimed contradictory things.

LAYOUT
  D:\.hermes\memories\shared\
    _clock.json              vector clock + per-node counters
    <Topic>/<node>-<ts>.md   one file per append (never rewritten)
    index.jsonl              append-only index of every entry

CLI
  python memory_bus.py --append --node claude --topic Research --text "..."
  python memory_bus.py --read [--topic T] [--node N] [--limit N]
  python memory_bus.py --topics
  python memory_bus.py --causality
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_HOME", r"D:\.hermes"))
SHARED = ROOT / "memories" / "shared"
CLOCK = SHARED / "_clock.json"
INDEX = SHARED / "index.jsonl"

# Hermes's own curated memory. Worker nodes must NEVER write here; the bus
# refuses paths under it so a bad --topic cannot escape the shared store.
PROTECTED = (ROOT / "memories" / "MEMORY.md", ROOT / "memories" / "USER.md")

SAFE = re.compile(r"[^A-Za-z0-9_\- ]+")


def _safe_name(s: str, default: str) -> str:
    s = SAFE.sub("", (s or "").strip()).strip().replace(" ", "_")
    return s[:64] or default


def _load_clock() -> dict:
    if CLOCK.exists():
        try:
            return json.loads(CLOCK.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"vector": {}, "entries": 0}


def _tick(node: str) -> dict:
    """Increment this node's counter and return a snapshot of the whole vector."""
    st = _load_clock()
    vec = st.setdefault("vector", {})
    vec[node] = int(vec.get(node, 0)) + 1
    st["entries"] = int(st.get("entries", 0)) + 1
    SHARED.mkdir(parents=True, exist_ok=True)
    CLOCK.write_text(json.dumps(st, indent=2), encoding="utf-8")
    return dict(vec)


def compare(a: dict, b: dict) -> str:
    """Vector-clock ordering: 'before', 'after', 'equal', or 'concurrent'."""
    keys = set(a) | set(b)
    le = all(a.get(k, 0) <= b.get(k, 0) for k in keys)
    ge = all(a.get(k, 0) >= b.get(k, 0) for k in keys)
    if le and ge:
        return "equal"
    if le:
        return "before"
    if ge:
        return "after"
    return "concurrent"


def append(node: str, topic: str, text: str, model: str = "",
           tags: list | None = None, source: str = "") -> dict:
    node = _safe_name(node, "unknown-node")
    topic = _safe_name(topic, "Inbox")
    if not (text or "").strip():
        raise SystemExit("refusing to append empty memory")

    tdir = (SHARED / topic).resolve()
    # Structural guarantee: the write must land inside the shared store.
    if not str(tdir).startswith(str(SHARED.resolve())):
        raise SystemExit(f"path escape refused: {tdir}")
    for p in PROTECTED:
        if tdir == p.resolve() or tdir in p.resolve().parents:
            raise SystemExit("refusing to write Hermes curated memory")

    created = not tdir.exists()
    tdir.mkdir(parents=True, exist_ok=True)

    vec = _tick(node)
    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%dT%H%M%S")
    fp = tdir / f"{node}-{stamp}.md"
    n = 1
    while fp.exists():
        n += 1
        fp = tdir / f"{node}-{stamp}-{n}.md"

    rec = {
        "ts": now.isoformat(),
        "node": node,
        "model": model or node,
        "topic": topic,
        "vector_clock": vec,
        "file": str(fp),
        "tags": tags or [],
        "source": source,
        "topic_created": created,
    }

    body = (
        f"---\n"
        f"node: {node}\nmodel: {model or node}\ntopic: {topic}\n"
        f"timestamp: {now.isoformat()}\n"
        f"vector_clock: {json.dumps(vec)}\n"
        f"tags: {json.dumps(tags or [])}\n"
        f"source: {source}\n"
        f"---\n\n{text.strip()}\n"
    )
    fp.write_text(body, encoding="utf-8")

    INDEX.parent.mkdir(parents=True, exist_ok=True)
    with INDEX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def read_entries(topic: str = "", node: str = "", limit: int = 50) -> list:
    if not INDEX.exists():
        return []
    out = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if topic and r.get("topic", "").lower() != topic.lower():
            continue
        if node and r.get("node", "").lower() != node.lower():
            continue
        out.append(r)
    return out[-limit:]


def causality() -> list:
    """Report pairs of concurrent (causally unordered) writes — contradiction risk."""
    ents = read_entries(limit=10_000)
    pairs = []
    for i in range(len(ents)):
        for j in range(i + 1, len(ents)):
            a, b = ents[i], ents[j]
            if a.get("topic") != b.get("topic"):
                continue
            if compare(a.get("vector_clock", {}), b.get("vector_clock", {})) == "concurrent":
                pairs.append((a, b))
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--read", action="store_true")
    ap.add_argument("--topics", action="store_true")
    ap.add_argument("--causality", action="store_true")
    # NB: default is empty, not "hermes". A default node name silently acted as a
    # READ FILTER, so `--read` matched nothing and reported "(no shared memory
    # yet)" while the index was full. Appends fall back to "hermes" explicitly.
    ap.add_argument("--node", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--topic", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.append:
        text = a.text
        if text == "-" or not text:
            text = sys.stdin.read()
        rec = append(a.node or "hermes", a.topic or "Inbox", text, a.model,
                     [t for t in a.tags.split(",") if t], a.source)
        print(json.dumps(rec, indent=2) if a.json else
              f"appended {rec['file']} clock={json.dumps(rec['vector_clock'])}")

    elif a.topics:
        SHARED.mkdir(parents=True, exist_ok=True)
        for d in sorted(p for p in SHARED.iterdir() if p.is_dir()):
            print(f"{d.name}  ({len(list(d.glob('*.md')))} entries)")

    elif a.causality:
        pairs = causality()
        if not pairs:
            print("No concurrent writes — memory is causally consistent.")
        for x, y in pairs:
            print(f"CONCURRENT in {x['topic']}: {x['node']}@{x['ts']} <> {y['node']}@{y['ts']}")

    elif a.read:
        ents = read_entries(a.topic, a.node, a.limit)
        if a.json:
            print(json.dumps(ents, indent=2))
        else:
            for r in ents:
                print(f"[{r['ts']}] {r['topic']}/{r['node']} ({r['model']}) -> {r['file']}")
            if not ents:
                print("(no shared memory yet)")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
