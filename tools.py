import asyncio
import os
from pathlib import Path
from typing import Dict, Any, List
import httpx
from workspace import workspace
from db import db


def _safe_resolve(project_dir: Path, rel_path: str) -> Path:
    """Гарантирует, что агент работает строго внутри выделенной папки проекта."""
    target = (project_dir / rel_path).resolve()
    proj_resolved = project_dir.resolve()
    if not str(target).startswith(str(proj_resolved)):
        raise ValueError(f"Доступ запрещен: путь '{rel_path}' выходит за пределы папки проекта!")
    return target


def truncate_output(text: str, max_chars: int = 3500) -> str:
    """Экономит токены, обрезая слишком длинные выводы посередине."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    removed = len(text) - max_chars
    return f"{text[:half]}\n\n... [Скрыто {removed} символов для экономии токенов] ...\n\n{text[-half:]}"


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Выполнить shell-команду внутри изолированной папки текущего проекта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Команда bash для выполнения"},
                    "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 45)", "default": 45},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать содержимое файла в папке проекта (с поддержкой диапазона строк).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу"},
                    "start_line": {"type": "integer", "description": "Начальная строка (с 1)", "default": 1},
                    "end_line": {"type": "integer", "description": "Конечная строка (по умолчанию 250)", "default": 250},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Создать или полностью перезаписать файл в папке проекта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу"},
                    "content": {"type": "string", "description": "Полное содержимое файла"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Точечно заменить фрагмент текста в файле (экономит токены).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу"},
                    "old_text": {"type": "string", "description": "Точный фрагмент для замены"},
                    "new_text": {"type": "string", "description": "Новый фрагмент текста"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск актуальной информации и документации в интернете через Tavily API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "max_results": {"type": "integer", "description": "Количество результатов (1-7)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_update",
            "description": "Записать важные сведения в долговременную память проекта (.agent_memory.md).",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Заметка или структура для сохранения"},
                    "append": {"type": "boolean", "description": "True — добавить в конец, False — перезаписать", "default": True},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Загрузить полную инструкцию скилла по его имени из списка доступных скиллов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя скилла"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_skill",
            "description": "Сохранить новый переиспользуемый скилл (инструкцию/шаблон) в библиотеку скиллов.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Короткое имя скилла латиницей"},
                    "description": {"type": "string", "description": "Однострочное описание, когда применять скилл"},
                    "instructions": {"type": "string", "description": "Подробная инструкция скилла в Markdown"},
                },
                "required": ["name", "description", "instructions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_action",
            "description": "Управление GitHub: создать новый репо, склонировать репо или сделать git push текущих изменений.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["push", "create_repo", "clone"],
                        "description": "Действие: push, create_repo или clone",
                    },
                    "repo_name": {"type": "string", "description": "Имя репо (или owner/repo для clone)"},
                    "commit_message": {"type": "string", "description": "Сообщение коммита для push"},
                    "private": {"type": "boolean", "description": "Приватный ли репозиторий при создании", "default": True},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_media",
            "description": "Прикрепить сгенерированный файл, скриншот или видео (.mp4, .png, .zip и т.д.) из папки проекта в чат пользователю.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Относительный путь к файлу в папке проекта"},
                    "caption": {"type": "string", "description": "Подпись к файлу/видео"},
                },
                "required": ["path"],
            },
        },
    },
]


async def execute_tool(
    project_name: str,
    tool_name: str,
    args: Dict[str, Any],
    media_queue: List[Dict[str, str]],
) -> str:
    project_dir = workspace.get_project_dir(project_name)

    try:
        if tool_name == "bash":
            cmd = args.get("command", "")
            timeout = min(int(args.get("timeout", 45)), 180)
            # Ограничиваем виртуальную память процесса (2 ГБ на Linux) и запускаем строго в папке проекта
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(project_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                decoded = out.decode("utf-8", errors="replace").strip()
                status = f"[exit code: {proc.returncode}]"
                return truncate_output(f"{status}\n{decoded}" if decoded else status)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Ошибка: команда превысила таймаут {timeout} с и была остановлена."

        elif tool_name == "read_file":
            target = _safe_resolve(project_dir, args["path"])
            if not target.exists():
                return f"Файл '{args['path']}' не существует."
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(args.get("start_line", 1)))
            end = min(len(lines), int(args.get("end_line", start + 250)))
            numbered = [f"{i}: {lines[i-1]}" for i in range(start, end + 1)]
            header = f"Файл: {args['path']} (строки {start}-{end} из {len(lines)})\n"
            return truncate_output(header + "\n".join(numbered))

        elif tool_name == "write_file":
            target = _safe_resolve(project_dir, args["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            target.write_text(content, encoding="utf-8")
            return f"Файл '{args['path']}' записан ({len(content)} байт)."

        elif tool_name == "edit_file":
            target = _safe_resolve(project_dir, args["path"])
            if not target.exists():
                return f"Файл '{args['path']}' не найден."
            text = target.read_text(encoding="utf-8", errors="replace")
            old_text = args["old_text"]
            new_text = args["new_text"]
            if old_text not in text:
                return "Ошибка edit_file: фрагмент old_text не найден в файле. Проверьте точные отступы через read_file."
            updated = text.replace(old_text, new_text, 1)
            target.write_text(updated, encoding="utf-8")
            return f"Файл '{args['path']}' успешно обновлен."

        elif tool_name == "web_search":
            tavily_key = await db.get_setting("tavily_api_key", "")
            if not tavily_key:
                return "Ошибка: Tavily API Key не настроен. Укажите его в /settings."
            query = args["query"]
            max_results = min(int(args.get("max_results", 5)), 7)
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": query,
                        "search_depth": "basic",
                        "include_answer": True,
                        "max_results": max_results,
                    },
                )
                if resp.status_code != 200:
                    return f"Ошибка Tavily API ({resp.status_code}): {resp.text[:300]}"
                data = resp.json()
                parts = []
                if data.get("answer"):
                    parts.append(f"Краткий ответ: {data['answer']}\n")
                for idx, item in enumerate(data.get("results", []), 1):
                    parts.append(
                        f"{idx}. {item.get('title')}\nURL: {item.get('url')}\n{item.get('content', '')[:500]}\n"
                    )
                return truncate_output("\n".join(parts) if parts else "Ничего не найдено.")

        elif tool_name == "memory_update":
            return workspace.update_memory(
                project_name,
                args.get("content", ""),
                append=bool(args.get("append", True)),
            )

        elif tool_name == "load_skill":
            return workspace.load_skill(args["name"])

        elif tool_name == "save_skill":
            return workspace.save_skill(args["name"], args["description"], args["instructions"])

        elif tool_name == "github_action":
            gh_token = await db.get_setting("github_token", "")
            action = args.get("action")
            if action == "push":
                msg = args.get("commit_message") or "Update by Telegram AI Agent"
                return await workspace.push_to_github(project_name, msg)
            elif action == "create_repo":
                rname = args.get("repo_name") or project_name
                ok, msg, new_proj = await workspace.create_github_repo(
                    gh_token, rname, private=bool(args.get("private", True))
                )
                if ok and new_proj:
                    await db.set_setting("active_project", new_proj)
                return msg
            elif action == "clone":
                rname = args.get("repo_name", "")
                ok, msg = await workspace.clone_github_repo(gh_token, rname)
                if ok:
                    slug = rname.split("/")[-1]
                    await db.set_setting("active_project", slug)
                return msg
            return f"Неизвестное действие github_action: {action}"

        elif tool_name == "send_media":
            target = _safe_resolve(project_dir, args["path"])
            if not target.exists():
                return f"Ошибка: файл '{args['path']}' не найден."
            media_queue.append({"path": str(target), "caption": args.get("caption", "")})
            return f"Файл '{args['path']}' добавлен в очередь отправки пользователю."

        else:
            return f"Неизвестный инструмент: {tool_name}"

    except Exception as e:
        return f"Ошибка выполнения {tool_name}: {e}"
