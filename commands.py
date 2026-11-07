"""
commands.py — Модуль команд и подсказок для Telegram-бота @claudeanalog1_bot.
Здесь собраны обработчики всех основных слэш-команд (/),
а также регистрация меню команд в интерфейсе Telegram (BotCommand).
"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from db import db
from workspace import workspace, sanitize_project_name

commands_router = Router()

# Список всех команд для регистрации в кнопке «Меню» Telegram
BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню и статус агента"),
    BotCommand(command="help", description="Справка по всем командам и возможностям"),
    BotCommand(command="model", description="Показать или сменить текущую модель"),
    BotCommand(command="projects", description="Список и выбор проектов (workspaces)"),
    BotCommand(command="new", description="Создать новый локальный проект"),
    BotCommand(command="session", description="Начать новую чистую сессию"),
    BotCommand(command="rollback", description="Откатить последний шаг (Git checkout)"),
    BotCommand(command="memory", description="Показать память проекта (.agent_memory.md)"),
    BotCommand(command="skills", description="Список доступных навыков (скиллов)"),
    BotCommand(command="status", description="Текущий статус, модель, токены и проект"),
    BotCommand(command="settings", description="Мастер настройки ключей и API"),
    BotCommand(command="stop", description="Прервать текущую выполняющуюся задачу"),
]


async def setup_bot_commands(bot):
    """Регистрирует команды в Telegram, чтобы они всплывали при вводе '/'."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        print(f"Не удалось установить команды бота: {e}")


@commands_router.message(Command("help"))
async def cmd_help(message: Message):
    me = await message.bot.get_me()
    text = (
        "📖 <b>Справочник по командам ИИ-агента:</b>\n\n"
        "<b>Основные команды:</b>\n"
        "• /start — Главный экран и статус\n"
        "• /status — Информация о текущем проекте, модели и API\n"
        "• /model — Просмотр и быстрая смена модели\n"
        "• /stop — Прервать выполнение задачи (кнопка ⏹)\n"
        "• /settings — Пошаговая настройка API-ключей\n\n"
        "<b>Управление проектами:</b>\n"
        "• /projects — Список изолированных проектов (workspaces/)\n"
        "• /new &lt;имя&gt; — Создать новый пустой проект\n"
        "• /session — Начать новую сессию диалога (сброс контекста)\n"
        "• /rollback — Откатить файлы к состоянию до последней задачи\n"
        "• /memory — Просмотр памяти проекта (<code>.agent_memory.md</code>)\n"
        "• /skills — Список доступных инструкций и шаблонов\n\n"
        f"<b>Инлайн-режим (@{me.username}):</b>\n"
        f"• <code>@{me.username} claude</code> — Поиск и смена модели из API\n"
        f"• <code>@{me.username} repo</code> — Поиск и клонирование ваших GitHub-репозиториев\n"
        f"• <code>@{me.username} new &lt;имя&gt;</code> — Создать проект или репо на GitHub\n"
        f"• <code>@{me.username} api &lt;url&gt; &lt;key&gt;</code> — Быстро подключить новый API\n\n"
        "<i>Вы можете отправлять текстовые задачи или голосовые сообщения!</i>"
    )
    await message.answer(text, parse_mode="HTML")


@commands_router.message(Command("status"))
async def cmd_status(message: Message):
    cfg = await db.get_active_llm_config()
    proj = await db.get_setting("active_project", "default")
    gh_token = await db.get_setting("github_token", "")
    tavily_key = await db.get_setting("tavily_api_key", "")
    groq_key = await db.get_setting("groq_api_key", "")

    gh_status = "✅ Подключен" if gh_token else "❌ Не настроен"
    tavily_status = "✅ Включен" if tavily_key else "❌ Не настроен"
    groq_status = "✅ Включен (Groq Whisper)" if groq_key else "❌ Не настроен"

    text = (
        "📊 <b>Текущий статус агента:</b>\n\n"
        f"📂 <b>Проект:</b> <code>{proj}</code> (<code>workspaces/{proj}</code>)\n"
        f"🧠 <b>Модель:</b> <code>{cfg['model']}</code>\n"
        f"🌐 <b>API URL:</b> <code>{cfg['base_url']}</code>\n"
        f"🐙 <b>GitHub:</b> {gh_status}\n"
        f"🔍 <b>Web Search:</b> {tavily_status}\n"
        f"🎤 <b>Голосовой ввод:</b> {groq_status}\n"
    )
    await message.answer(text, parse_mode="HTML")


@commands_router.message(Command("model"))
async def cmd_model(message: Message):
    cfg = await db.get_active_llm_config()
    models = await db.list_saved_models()
    buttons = []
    for m in models[:10]:
        mid = m["model_id"]
        is_active = mid == cfg["model"]
        prefix = "✅ " if is_active else "🧠 "
        buttons.append([InlineKeyboardButton(text=f"{prefix}{mid}", callback_data=f"set_model:{mid}")])

    me = await message.bot.get_me()
    text = (
        f"🧠 <b>Текущая модель:</b> <code>{cfg['model']}</code>\n"
        f"Провайдер: <code>{cfg['provider']}</code> ({cfg['base_url']})\n\n"
        f"Выберите из сохраненных или используйте поиск через <code>@{me.username} &lt;модель&gt;</code>:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@commands_router.message(Command("new"))
async def cmd_new(message: Message):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите имя проекта, например:\n<code>/new my_super_bot</code>", parse_mode="HTML")
        return
    proj_name = sanitize_project_name(parts[1])
    await workspace.ensure_project_initialized(proj_name)
    await db.set_setting("active_project", proj_name)
    await db.start_new_session(proj_name)
    await message.answer(
        f"📁 <b>Создан и активирован проект:</b> <code>{proj_name}</code>\n"
        f"Папка: <code>workspaces/{proj_name}</code>\n"
        f"Память и локальный Git инициализированы.",
        parse_mode="HTML",
    )


@commands_router.message(Command("session"))
async def cmd_session(message: Message):
    proj = await db.get_setting("active_project", "default")
    new_sid = await db.start_new_session(proj)
    await message.answer(
        f"✨ <b>Начата новая чистая сессия (#{new_sid})</b> для проекта <code>{proj}</code>.\n"
        "Контекст прошлых запросов сброшен для экономии токенов, файлы и память проекта сохранены.",
        parse_mode="HTML",
    )


@commands_router.message(Command("rollback"))
async def cmd_rollback(message: Message):
    proj = await db.get_setting("active_project", "default")
    sid = await db.get_or_create_active_session(proj)
    last_turn = await db.pop_last_turn(sid)
    if not last_turn:
        await message.answer("История шагов пуста — нечего откатывать.")
        return
    ok, msg = await workspace.rollback_to_checkpoint(proj, last_turn["pre_commit_sha"])
    if ok:
        await message.answer(f"⏪ <b>Откат выполнен:</b>\n{msg}\nЗадача: <i>{last_turn['user_prompt'][:100]}</i>", parse_mode="HTML")
    else:
        await message.answer(f"❌ Ошибка отката: {msg}")


@commands_router.message(Command("memory"))
async def cmd_memory(message: Message):
    proj = await db.get_setting("active_project", "default")
    mem = workspace.read_memory(proj)
    if not mem:
        await message.answer(f"Память проекта <code>{proj}</code> пока пуста.", parse_mode="HTML")
        return
    await message.answer(f"📝 <b>Память проекта ({proj}/.agent_memory.md):</b>\n\n<pre>{mem}</pre>", parse_mode="HTML")


@commands_router.message(Command("skills"))
async def cmd_skills(message: Message):
    skills_text = workspace.list_skills_compact()
    await message.answer(f"🛠 <b>Доступные скиллы (skills/):</b>\n\n{skills_text}", parse_mode="HTML")
