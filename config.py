import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Строгий доступ только для этого Telegram ID (Админ)
ADMIN_ID = int(os.getenv("ADMIN_ID", "5193621219"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
WORKSPACES_DIR = BASE_DIR / "workspaces"
SKILLS_DIR = BASE_DIR / "skills"
DB_PATH = DATA_DIR / "agent.db"

# Создаем необходимые системные папки
for directory in (DATA_DIR, WORKSPACES_DIR, SKILLS_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def get_bot_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("\n=== ПЕРВЫЙ ЗАПУСК СЕРВЕРА ===")
        token = input("Введите TELEGRAM_BOT_TOKEN от @BotFather: ").strip()
        with open(BASE_DIR / ".env", "a", encoding="utf-8") as f:
            f.write(f"\nTELEGRAM_BOT_TOKEN={token}\n")
    return token


def init_default_skills():
    """Создает базовые скиллы при первом запуске, если папка skills пуста."""
    debugger_skill = SKILLS_DIR / "systematic_debugging.md"
    if not debugger_skill.exists():
        debugger_skill.write_text(
            "---\n"
            "name: systematic_debugging\n"
            "description: Пошаговый поиск и исправление сложных багов с минимальным расходом токенов\n"
            "---\n"
            "# Инструкция по отладке\n"
            "1. Сначала воспроизведи проблему или прочитай точный traceback.\n"
            "2. Используй `read_file` с указанием диапазона строк вместо чтения огромных файлов целиком.\n"
            "3. Вноси точечные правки через `edit_file` и сразу проверяй результат тестом через `bash`.\n",
            encoding="utf-8",
        )

    git_skill = SKILLS_DIR / "github_workflow.md"
    if not git_skill.exists():
        git_skill.write_text(
            "---\n"
            "name: github_workflow\n"
            "description: Работа с GitHub репозиториями, ветками и коммитами\n"
            "---\n"
            "# Работа с Git и GitHub\n"
            "1. Перед пушем проверь `git status` и `git diff --stat` через `bash`.\n"
            "2. Используй инструмент `github_push` для отправки коммита на GitHub или `github_create_repo` для создания нового репозитория.\n",
            encoding="utf-8",
        )
