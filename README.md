# Telegram AI Coding Agent

Autonomous, self-hosted AI developer in Telegram powered by aiogram, SQLite, and OpenAI-compatible LLM endpoints. Features real-time live drafts streaming, workspace sandboxing, automated Git checkpointing for instant rollback, and direct GitHub integration.

[English](#features) | [Русский](#возможности)

---

## Features

- **ReAct Execution Engine**: Autonomous step-by-step tool invocation (`bash`, `read_file`, `edit_file`, `write_file`, `glob`, `grep`, `fetch_web`, `web_search`, `github_action`).
- **Live Draft Streaming**: Real-time drafting in Telegram via `sendMessageDraft` / live message edits showing execution logs, time elapsed, and token count.
- **Project Workspaces**: Each project or new session gets an isolated working directory with dedicated Git checkpoints and persistent memory.
- **GitHub Integration**: Inline repository search (`@bot repo ...`), automatic branch discovery, and one-click cloning or remote repository creation.
- **Project Guidelines**: Dynamic auto-injection of `CLAUDE.md` and `AGENT.md` guidelines directly into the agent's prompt context.
- **Voice Input**: Speech-to-text powered by Groq Whisper large-v3.
- **Strict Minimalist Output**: Clean telegraphic formatting with zero emojis and proper code blocks.
- **Security & Privacy**: Access strictly gated to a single Telegram user ID. Sensitive keys stored safely in SQLite or `.env`.

---

## Quick Start

### 1. Requirements
- Python 3.10+
- Git

### 2. Installation
```bash
git clone https://github.com/Gl1ch5/tg-agent.git
cd tg-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration
Copy sample environment file:
```bash
cp .env.example .env
```
Fill in your credentials in `.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
ADMIN_ID=your_telegram_numeric_id
```

### 4. Run
```bash
python3 bot.py
```
Or configure systemd:
```bash
sudo cp tg-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-agent
```

---

## Bot Commands & Inline Mode

- `/start` — Main control panel with interactive inline buttons.
- `/session` — Create clean isolated sessions (local or cloned from GitHub).
- `/projects` — Switch between isolated project folders and previous sessions.
- `/init` — Generate `CLAUDE.md` and `AGENT.md` project blueprints.
- `/config` — View and edit active model, API endpoints, and tokens.
- `/rollback` — Instantly roll back the workspace to the state before the last agent turn.
- `/stop` — Terminate current running task.

**Inline mode (`@bot_username`):**
- `@bot <model>` — Live search and switch LLM models.
- `@bot repo <search>` — Search GitHub repositories with branch selection.
- `@bot new <name>` — Create an isolated local project or private GitHub repo.
- `@bot api <url> <key>` — Quickly connect a custom LLM provider endpoint.

---

## Architecture

```
tg_agent/
├── bot.py             # Telegram interface, inline handlers, message split & drafts
├── agent.py           # ReAct engine & streaming LLM completion client
├── tools.py           # Tool definitions, execution & output truncation
├── workspace.py       # Isolated workspace directories, Git checkpoints & GitHub API
├── db.py              # SQLite storage for sessions, history turns & settings
├── formatter.py       # Markdown to Telegram HTML converter & emoji filter
├── config.py          # Environment settings loader
├── SYSTEM_PROMPT.md   # System prompt specification (RU / EN)
├── CLAUDE.md          # Architecture guide
└── AGENT.md           # Agent specification
```

---

## Русский

## Возможности

- **ReAct-движок**: Автономное пошаговое выполнение действий через инструменты (`bash`, `read_file`, `edit_file`, `write_file`, `glob`, `grep`, `fetch_web`, `web_search`, `github_action`).
- **Стриминг в черновик**: Живой вывод хода мыслей и шагов в реальном времени через `sendMessageDraft` с таймером и счетчиком токенов.
- **Изоляция проектов**: Каждая сессия или проект изолируется в отдельной папке `workspaces/` с локальным Git и памятью `.agent_memory.md`.
- **Интеграция с GitHub**: Инлайн-поиск репозиториев (`@bot repo ...`), выбор веток и клонирование в один клик.
- **Автозагрузка инструкций**: Автоматическое считывание `CLAUDE.md` и `AGENT.md` из папки проекта в системный промпт.
- **Голосовой ввод**: Распознавание голосовых сообщений через Groq Whisper.
- **Строгий технический стиль**: Полное отсутствие цветных эмодзи, лаконичные отчеты и подсветка кода.

## Установка и запуск

```bash
git clone https://github.com/Gl1ch5/tg-agent.git
cd tg-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполните TELEGRAM_BOT_TOKEN и ADMIN_ID в .env
python3 bot.py
```

## Лицензия
MIT
