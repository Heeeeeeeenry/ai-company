# AI Company (狗蛋儿)

Multi-agent virtual software company powered by LangGraph + DeepSeek.

## Features
- Multi-agent workflow (CEO → PM → Architect → Developer → Auditor → PMO)
- Web search (Tavily), market data, stock/forex queries
- Multi-session system with persistent per-session memory
- Global shared memory across all sessions
- Visual Context Engine (screenshot capture + Qwen-VL vision analysis)
- WeChat message sending via AppleScript (macOS)
- Auto-summarize conversations + skill learning from successful tasks
- Token usage tracking per session
- Local system detection (pgrep/osascript)

## Quick Start

```bash
cp .env.example .env
# Edit .env with your API keys
pip install -r requirements.txt
python3 -m src.main --mode cli
```

## CLI Commands

| Command | Description |
|---------|-------------|
| /help | Show all commands |
| /token, /usage | Session token usage |
| /sessions | List all sessions |
| /session new | Create session |
| /session switch | Switch session |
| /vision scan | Screen capture + analyze |
| /vision status | VCE status |
| /global set/get | Global memory |
| /quit | Exit |

## Architecture

```
User Input → CEO (LangGraph)
  ├─ Triage → classifies task type
  ├─ PM → Architect → Execute
  ├─ CapabilityPlanner → DepartmentAgent (ReAct loop)
  ├─ Auditor → PMO → Verify → Deliver
  └─ Auto-Repair on FAIL
```

## Session System

Each session has independent persistent memory stored in `~/.ai-company/sessions/`.
Global memories (principles, configs) shared across all sessions.

## Visual Context Engine (VCE)

Passive screen observation: screenshots → Qwen-VL vision analysis → structured memory.
CLI-first: uses pgrep/osascript when possible before falling back to vision.
