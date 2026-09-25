![Hermes Orchestrator](https://img.shields.io/badge/Hermes-Orchestrator-6366F1?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

**Multi-agent orchestration for Hermes — coordinate specialized AI agents across a swarm.**

---

## 🏗️ Architecture

```mermaid
graph TB
    subgraph Control Plane
        API[REST API<br/>FastAPI]
        SCH[Scheduler<br/>Priority Queue]
    end
    subgraph Specialists
        W1[Writer Agent]
        R1[Reviewer Agent]
        C1[Coder Agent]
        E1[Executor Agent]
    end
    subgraph Shared Memory
        BB[(Blackboard<br/>State Store)]
        LOG[(Event Log)]
    end
    subgraph Governor
        GV[Resource Governor<br/>RAM Admission]
    end
    API --> SCH
    SCH --> W1
    SCH --> R1
    SCH --> C1
    SCH --> E1
    W1 <--> BB
    R1 <--> BB
    C1 <--> BB
    E1 <--> BB
    GV --> SCH
    W1 --> LOG
    R1 --> LOG
    C1 --> LOG
    E1 --> LOG
```

---

## ✨ Features

- **Specialist agents** — writer, reviewer, coder, executor with distinct tool sets
- **Shared blackboard** — agents read/write common state for coordination
- **Resource governor** — RAM-based admission control to prevent OOM
- **Event logging** — full audit trail of agent actions and decisions
- **Priority scheduling** — critical tasks jump the queue

---

## 🚀 Quick Start

```bash
pip install -r requirements.txt
python -m orchestrator.swarm --scale x --profile default
```

---

## 📁 Project Structure

```
hermes-orchestrator/
├── orchestrator/
│   ├── swarm.py           # Main swarm orchestrator
│   ├── scheduler.py       # Priority queue + dispatch
│   ├── blackboard.py      # Shared state store
│   ├── governor.py        # Resource governor
│   ├── agents/            # Specialist agent implementations
│   └── log.py             # Event logging
├── tests/
└── README.md
```

---

## 📄 License

MIT © Md Sadman Bin Masud
