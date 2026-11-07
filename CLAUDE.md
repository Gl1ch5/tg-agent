# CLAUDE.md - Telegram AI Agent Project Guide

## Overview
Self-hosted Telegram ReAct coding & task agent (@claudeanalog1_bot) running on Python 3.12 with aiogram and OpenAI-compatible LLM backends.

## Architecture
- `bot.py` - Telegram bot lifecycle, message routing, streaming updates into drafts, error handling, inline queries.
- `agent.py` - ReAct engine (`run_react_agent`), LLM streaming calls (`stream_llm_step`), message history compression. Unlimited steps loop.
- `tools.py` - Tools schema and execution: bash, read_file, write_file, edit_file, glob, grep, fetch_web, web_search, manage_workspace.
- `workspace.py` - Isolated directory workspaces (`workspaces/proj_*`), git checkpointing on each step/turn, persistent memory files.
- `formatter.py` - Strict formatting rules: emoji stripping, clean telegraphic status indicators (`▪`, `›`, `▸`), HTML escaping.
- `commands.py` - Telegram slash commands (`/start`, `/help`, `/project`, `/model`, `/checkpoint`, etc.).
- `db.py` - SQLite persistence for models, sessions, turns, and workspace states.
- `config.py` - Environment configuration loaded from `.env`.

## Service & Management
- Systemd service: `tg-agent.service`
- Restart service: `systemctl restart tg-agent.service`
- Check logs: `journalctl -u tg-agent.service -f -n 50`
- Python venv: `/opt/nanobot/workspace/tg_agent/venv`
- Admin Telegram ID: `5193621219`

## Coding Style & Rules
- Minimalistic, clean telegraphic style without decorative emojis.
- Never hardcode sensitive tokens; use `.env` or the SQLite config.
- Tool output truncation and economy handling are handled in `tools.py` and `agent.py`.
