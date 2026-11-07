import asyncio
import html
import io
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    Update,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)

from config import ADMIN_ID, get_bot_token, init_default_skills
from db import db
from workspace import workspace, sanitize_project_name
from agent import run_react_agent
from formatter import md_to_tg_html

ACTIVE_TASKS: Dict[int, asyncio.Task] = {}
ONLINE_MODELS_CACHE: Dict[str, Any] = {"ts": 0, "items": []}


async def setup_bot_commands(bot: Bot):
    """Регистрирует меню команд и всплывающие подсказки при вводе '/'."""
    commands = [
        BotCommand(command="start", description="Главная панель управления"),
        BotCommand(command="session", description="Новая сессия (без GitHub или из репо)"),
        BotCommand(command="init", description="Инициализировать CLAUDE.md / AGENT.md"),
        BotCommand(command="config", description="Конфигурация и параметры агента"),
        BotCommand(command="projects", description="Список изолированных проектов"),
        BotCommand(command="model", description="Сменить модель (инлайн поиск)"),
        BotCommand(command="rollback", description="Откатить последний шаг (git)"),
        BotCommand(command="stop", description="Остановить генерацию"),
        BotCommand(command="settings", description="Мастер настройки ключей"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception:
        pass


class AdminAndStopMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: Dict[str, Any]):
        raw_extra = getattr(event, "model_extra", None) or {}
        if "stopped_message_generation" in raw_extra:
            task = ACTIVE_TASKS.get(ADMIN_ID)
            if task and not task.done():
                task.cancel()
            return

        user = None
        if event.message:
            user = event.message.from_user
        elif event.callback_query:
            user = event.callback_query.from_user
        elif event.inline_query:
            user = event.inline_query.from_user

        if user and user.id != ADMIN_ID:
            if event.message:
                await event.message.answer("[x] Доступ закрыт.")
            elif event.callback_query:
                await event.callback_query.answer("[x] Нет доступа", show_alert=True)
            return

        return await handler(event, data)


class SetupWizard(StatesGroup):
    waiting_llm_url = State()
    waiting_llm_key = State()
    waiting_tavily_key = State()
    waiting_groq_key = State()
    waiting_github_token = State()


def get_url_presets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▸ OpenRouter", callback_data="preset_url:https://openrouter.ai/api/v1"),
                InlineKeyboardButton(text="▸ OpenAI", callback_data="preset_url:https://api.openai.com/v1"),
            ],
            [
                InlineKeyboardButton(text="▸ DeepSeek", callback_data="preset_url:https://api.deepseek.com/v1"),
                InlineKeyboardButton(text="▸ Groq", callback_data="preset_url:https://api.groq.com/openai/v1"),
            ],
        ]
    )


def get_skip_kb(step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="› пропустить шаг", callback_data=f"skip_setup:{step}")]]
    )


def get_final_report_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="↻ повторить", callback_data="agent_action:retry"),
                InlineKeyboardButton(text="↶ откатить", callback_data="agent_action:rollback"),
                InlineKeyboardButton(text="+ новая", callback_data="agent_action:new_session"),
            ]
        ]
    )


class TelegramDraftStreamer:
    def __init__(self, bot_token: str, chat_id: int):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.draft_id = int(time.time() * 1000) % 2_000_000_000 or 1
        self.last_update_ts = 0.0
        self.supports_native_draft = True
        self.fallback_msg_id: Optional[int] = None
        self.client = httpx.AsyncClient(timeout=10.0)

    def _build_draft_text(self, steps_log: List[str], partial_text: str, elapsed: float, max_len: int = 3900) -> str:
        header = f"<i>работаю... {int(elapsed)} с</i>"
        if steps_log:
            recent_steps = "\n".join(html.escape(s) for s in steps_log[-5:])
            header += f"\n<blockquote expandable>{recent_steps}</blockquote>"

        header_len = len(header) + 4
        available_len = max(500, max_len - header_len)
        body = md_to_tg_html(partial_text[-available_len:], is_streaming=True) if partial_text else ""
        combined = f"{header}\n\n{body}".strip()
        if len(combined) > max_len:
            combined = combined[:max_len]
        return combined

    async def update_draft(self, steps_log: List[str], partial_text: str, elapsed: float, tokens: int, force: bool = False):
        now = time.time()
        if not force and (now - self.last_update_ts) < 0.8:
            return
        self.last_update_ts = now

        full_draft = self._build_draft_text(steps_log, partial_text, elapsed, max_len=3800)

        if self.supports_native_draft:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessageDraft"
            payload = {
                "chat_id": self.chat_id,
                "draft_id": self.draft_id,
                "text": full_draft,
                "parse_mode": "HTML",
                "can_stop": True,
                "keep_on_stop": True,
            }
            try:
                resp = await self.client.post(url, json=payload)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return
                payload.pop("can_stop", None)
                payload.pop("keep_on_stop", None)
                resp2 = await self.client.post(url, json=payload)
                if resp2.status_code == 200 and resp2.json().get("ok"):
                    return
                self.supports_native_draft = False
            except Exception:
                self.supports_native_draft = False

        if self.fallback_msg_id is None:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            r = await self.client.post(url, json={"chat_id": self.chat_id, "text": full_draft, "parse_mode": "HTML"})
            if r.status_code == 200 and r.json().get("ok"):
                self.fallback_msg_id = r.json()["result"]["message_id"]
        else:
            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            await self.client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "message_id": self.fallback_msg_id,
                    "text": full_draft,
                    "parse_mode": "HTML",
                },
            )

    async def close(self, bot: Bot):
        if self.fallback_msg_id:
            try:
                await bot.delete_message(self.chat_id, self.fallback_msg_id)
            except Exception:
                pass
        await self.client.aclose()


async def transcribe_voice_groq(bot: Bot, file_id: str) -> str:
    groq_key = await db.get_setting("groq_api_key", "")
    if not groq_key:
        raise RuntimeError("Не задан Groq API Key. Укажите его в /settings.")

    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    buf.seek(0)

    async with httpx.AsyncClient(timeout=45.0) as client:
        files = {"file": ("voice.ogg", buf.read(), "audio/ogg")}
        data = {"model": "whisper-large-v3-turbo", "response_format": "json"}
        headers = {"Authorization": f"Bearer {groq_key}"}
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Groq Whisper Error ({resp.status_code}): {resp.text[:250]}")
        return resp.json().get("text", "").strip()


async def search_models_online(query: str) -> List[Dict[str, Any]]:
    now = time.time()
    llm_cfg = await db.get_active_llm_config()
    saved = await db.list_saved_models(query)

    if now - ONLINE_MODELS_CACHE["ts"] > 90 or not ONLINE_MODELS_CACHE["items"]:
        try:
            headers = {"Authorization": f"Bearer {llm_cfg['api_key']}"} if llm_cfg["api_key"] else {}
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{llm_cfg['base_url']}/models", headers=headers)
                if r.status_code == 200:
                    raw_list = r.json().get("data", [])
                    ONLINE_MODELS_CACHE["items"] = [
                        {
                            "model_id": m.get("id", ""),
                            "display_name": m.get("name") or m.get("id", ""),
                            "context_length": m.get("context_length") or 128000,
                            "provider": llm_cfg["provider"],
                        }
                        for m in raw_list
                        if m.get("id")
                    ]
                    ONLINE_MODELS_CACHE["ts"] = now
        except Exception:
            pass

    q = query.lower().strip()
    seen = {m["model_id"] for m in saved}
    combined = list(saved)

    for m in ONLINE_MODELS_CACHE["items"]:
        if m["model_id"] in seen:
            continue
        if not q or q in m["model_id"].lower() or q in m["display_name"].lower():
            combined.append(m)
            if len(combined) >= 35:
                break

    return combined[:35]


async def send_formatted_report(bot: Bot, chat_id: int, header_block: str, raw_report: str):
    """Форматирует финальный ответ через md_to_tg_html и безопасно отправляет с фоллбэком."""
    formatted_body = md_to_tg_html(raw_report, is_streaming=False)
    final_text = header_block + formatted_body
    if len(final_text) > 4090:
        final_text = header_block + html.escape(raw_report[:3800]) + "\n..."

    async def split_and_send(chat_id: int, text: str, markup=None):
        """Безопасно разбивает слишком длинные сообщения на части до 4000 символов."""
        max_chunk = 4000
        if len(text) <= max_chunk:
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                safe = html.escape(text)
                await bot.send_message(
                    chat_id,
                    safe[:max_chunk],
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return

        # Если текст длиннее 4000 символов, разбиваем по строкам
        lines = text.split("\n")
        current_chunk = ""
        chunks = []
        for line in lines:
            if len(current_chunk) + len(line) + 1 > max_chunk:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = f"{current_chunk}\n{line}" if current_chunk else line
        if current_chunk:
            chunks.append(current_chunk)

        for i, chunk in enumerate(chunks):
            # Кнопки прикрепляем только к последнему сообщению
            kb = markup if i == len(chunks) - 1 else None
            try:
                await bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                await bot.send_message(
                    chat_id,
                    html.escape(chunk[:max_chunk]),
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )

    await split_and_send(chat_id, final_text, get_final_report_kb())


async def execute_user_prompt(bot: Bot, chat_id: int, prompt: str):
    if chat_id in ACTIVE_TASKS and not ACTIVE_TASKS[chat_id].done():
        await bot.send_message(chat_id, "▪ Агент уже занят. Нажмите стоп-кнопку или /stop.")
        return

    project_name = await db.get_setting("active_project", "default")
    await workspace.ensure_project_initialized(project_name)
    session_id = await db.get_or_create_active_session(project_name)
    streamer = TelegramDraftStreamer(bot.token, chat_id)

    async def _task_runner():
        start_ts = time.time()
        last_steps: List[str] = []
        last_partial = ""
        last_tokens = 0
        ticker_running = True

        async def _ticker():
            while ticker_running:
                await streamer.update_draft(last_steps, last_partial, time.time() - start_ts, last_tokens)
                await asyncio.sleep(1.0)

        ticker_task = asyncio.create_task(_ticker())

        async def on_progress(steps: List[str], partial: str, elapsed: float, tokens: int):
            nonlocal last_steps, last_partial, last_tokens
            last_steps = list(steps)
            if partial:
                last_partial = partial
            last_tokens = tokens
            await streamer.update_draft(last_steps, last_partial, elapsed, last_tokens)

        try:
            result = await run_react_agent(
                project_name=project_name,
                session_id=session_id,
                user_prompt=prompt,
                on_progress=on_progress,
            )
            ticker_running = False
            ticker_task.cancel()
            await streamer.close(bot)

            elapsed_str = f"{result['elapsed']:.1f}"
            tokens_str = f"{result['tokens']:,}"
            steps_text = "\n".join(html.escape(s) for s in result["steps_log"]) if result["steps_log"] else "▸ прямой ответ"
            header_block = (
                f"<blockquote expandable>Готово • {elapsed_str} с • {tokens_str} токенов\n"
                f"проект: {html.escape(project_name)}\n"
                f"{steps_text}</blockquote>\n\n"
            )

            for item in result.get("media", []):
                mpath = Path(item["path"])
                if not mpath.exists():
                    continue
                caption = item.get("caption") or mpath.name
                if mpath.suffix.lower() in (".mp4", ".mov", ".webm"):
                    await bot.send_video(chat_id, FSInputFile(str(mpath)), caption=caption)
                elif mpath.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    await bot.send_photo(chat_id, FSInputFile(str(mpath)), caption=caption)
                else:
                    await bot.send_document(chat_id, FSInputFile(str(mpath)), caption=caption)

            await send_formatted_report(bot, chat_id, header_block, result["report"])

        except asyncio.CancelledError:
            ticker_running = False
            ticker_task.cancel()
            await streamer.close(bot)
            await bot.send_message(
                chat_id,
                f"<i>остановлено • {time.time() - start_ts:.1f} с</i>\n\n{md_to_tg_html(last_partial[:1500])}",
                parse_mode="HTML",
                reply_markup=get_final_report_kb(),
            )
        except Exception as e:
            ticker_running = False
            ticker_task.cancel()
            await streamer.close(bot)
            await bot.send_message(
                chat_id,
                f"<b>[ошибка выполнения]</b>\n<pre><code>{html.escape(str(e)[:1500])}</code></pre>",
                parse_mode="HTML",
                reply_markup=get_final_report_kb(),
            )

    task = asyncio.create_task(_task_runner())
    ACTIVE_TASKS[chat_id] = task


dp = Dispatcher()
dp.update.outer_middleware(AdminAndStopMiddleware())


async def start_onboarding(message: Message, state: FSMContext):
    await state.set_state(SetupWizard.waiting_llm_url)
    await message.answer(
        "<b>▪ Первичная настройка агента</b>\n"
        "<code>────────────────────────</code>\n"
        "Шаг 1/5 • Выберите <b>LLM Base URL</b> кнопкой ниже или отправьте свой адрес:",
        parse_mode="HTML",
        reply_markup=get_url_presets_kb(),
    )


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not await db.is_configured():
        await start_onboarding(message, state)
        return

    cfg = await db.get_active_llm_config()
    proj = await db.get_setting("active_project", "default")
    sid = await db.get_or_create_active_session(proj)
    me = await message.bot.get_me()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✨ Новая сессия", callback_data="menu:new_session_dialog"),
                InlineKeyboardButton(text="📁 Проекты", callback_data="menu:projects"),
            ],
            [
                InlineKeyboardButton(text="🧠 Модель", switch_inline_query_current_chat=""),
                InlineKeyboardButton(text="🐙 GitHub Репозитории", switch_inline_query_current_chat="repo "),
            ],
            [
                InlineKeyboardButton(text="⚙️ Конфиг / Настройки", callback_data="menu:config"),
                InlineKeyboardButton(text="📄 /init CLAUDE.md", callback_data="menu:init_guide"),
            ],
            [
                InlineKeyboardButton(text="↶ Откатить шаг", callback_data="agent_action:rollback"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="menu:stop"),
            ],
        ]
    )

    await message.answer(
        f"<b>▪ Панель управления // Agent</b>\n"
        f"<code>────────────────────────</code>\n"
        f"• Проект: <code>{proj}</code> (сессия #{sid})\n"
        f"• Модель: <code>{cfg['model']}</code>\n"
        f"• Провайдер: <code>{cfg['provider']}</code>\n\n"
        f"Используйте кнопки под сообщением для мгновенного управления или инлайн-поиска:",
        parse_mode="HTML",
        reply_markup=kb,
    )


def get_new_session_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▫️ Новая сессия (без GitHub)", callback_data="sess_create:local"),
            ],
            [
                InlineKeyboardButton(text="🐙 Выбрать GitHub репозиторий", switch_inline_query_current_chat="repo "),
            ],
            [
                InlineKeyboardButton(text="➕ Создать новый репо на GitHub", switch_inline_query_current_chat="new "),
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data="menu:start"),
            ],
        ]
    )


@dp.message(Command("session"))
@dp.message(Command("new"))
async def cmd_new_session_menu(message: Message):
    proj = await db.get_setting("active_project", "default")
    await message.answer(
        f"<b>▪ Создание / смена сессии</b>\n"
        f"Текущий проект: <code>{proj}</code>\n\n"
        f"Выберите вариант запуска:",
        parse_mode="HTML",
        reply_markup=get_new_session_kb(),
    )


@dp.message(Command("init"))
async def cmd_init(message: Message):
    proj = await db.get_setting("active_project", "default")
    p_dir = workspace.get_project_dir(proj)
    created = []
    c_path = p_dir / "CLAUDE.md"
    a_path = p_dir / "AGENT.md"

    if not c_path.exists():
        c_path.write_text(
            f"# CLAUDE.md - {proj}\n\n"
            f"## Руководство по проекту\n"
            f"- Имя проекта: `{proj}`\n"
            f"- Описание и архитектура:\n"
            f"- Основные команды:\n\n"
            f"## Стиль кода\n"
            f"- Без эмодзи, лаконичный чистый код.\n",
            encoding="utf-8",
        )
        created.append("CLAUDE.md")

    if not a_path.exists():
        a_path.write_text(
            f"# AGENT.md - {proj}\n\n"
            f"## Спецификация агента\n"
            f"- Автоматически подгружается в системный контекст агента.\n"
            f"- Роль: Ведущий инженер проекта {proj}.\n"
            f"- Задачи:\n",
            encoding="utf-8",
        )
        created.append("AGENT.md")

    if created:
        msg = f"<b>▪ Файлы инициализированы в <code>workspaces/{proj}</code>:</b>\n" + "\n".join(f"• <code>{f}</code>" for f in created)
    else:
        msg = f"<b>▪ Файлы уже существуют в <code>workspaces/{proj}</code>:</b>\n• <code>CLAUDE.md</code>\n• <code>AGENT.md</code>"
    msg += "\n\n<i>Они автоматически подгружаются в память и системный промпт агента!</i>"
    await message.answer(msg, parse_mode="HTML")


@dp.message(Command("config"))
async def cmd_config(message: Message):
    cfg = await db.get_active_llm_config()
    proj = await db.get_setting("active_project", "default")
    gh_token = await db.get_setting("github_token", "")
    tavily = await db.get_setting("tavily_api_key", "")
    groq = await db.get_setting("groq_api_key", "")

    gh_info = "подключен (ghp_...)" if gh_token else "не задан"
    tav_info = "активен (tvly-...)" if tavily else "не задан"
    grq_info = "активен (Whisper)" if groq else "не задан"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 Сменить модель", switch_inline_query_current_chat=""),
                InlineKeyboardButton(text="🐙 Сменить репо", switch_inline_query_current_chat="repo "),
            ],
            [
                InlineKeyboardButton(text="🔑 Изменить API ключи (/settings)", callback_data="menu:reconfigure"),
                InlineKeyboardButton(text="« В главное меню", callback_data="menu:start"),
            ],
        ]
    )

    text = (
        f"<b>⚙️ Конфигурация агента:</b>\n"
        f"<code>────────────────────────</code>\n"
        f"• Активный проект: <code>{proj}</code>\n"
        f"• Модель: <code>{cfg['model']}</code>\n"
        f"• Провайдер: <code>{cfg['provider']}</code>\n"
        f"• Base URL: <code>{cfg['base_url']}</code>\n"
        f"• GitHub Token: <i>{gh_info}</i>\n"
        f"• Tavily Web Search: <i>{tav_info}</i>\n"
        f"• Groq Whisper: <i>{grq_info}</i>\n"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("model"))
async def cmd_model(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        model_id = parts[1].strip()
        await db.save_model(model_id)
        await db.set_setting("active_model", model_id)
        await message.answer(f"<b>▪ Модель переключена:</b> <code>{html.escape(model_id)}</code>", parse_mode="HTML")
        return

    cfg = await db.get_active_llm_config()
    me = await message.bot.get_me()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▸ Открыть поиск моделей", switch_inline_query_current_chat="")]
        ]
    )
    await message.answer(
        f"<b>▪ Текущая модель:</b> <code>{cfg['model']}</code>\n\n"
        f"Нажмите кнопку ниже для живого поиска по API или введите <code>/model id_модели</code>:",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.message(Command("rollback"))
async def cmd_rollback(message: Message):
    project_name = await db.get_setting("active_project", "default")
    session_id = await db.get_or_create_active_session(project_name)
    last_turn = await db.pop_last_turn(session_id)
    if not last_turn:
        await message.answer("▪ Нечего откатывать — история сессии пуста.")
        return
    _, msg = await workspace.rollback_to_checkpoint(project_name, last_turn["pre_commit_sha"])
    await message.answer(f"<b>↶ Откат выполнен</b>\n{md_to_tg_html(msg)}", parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    task = ACTIVE_TASKS.get(message.chat.id)
    if task and not task.done():
        task.cancel()
    else:
        await message.answer("▪ Нет активных задач.")


@dp.message(Command("projects"))
async def cmd_projects(message: Message):
    active = await db.get_setting("active_project", "default")
    projs = workspace.list_projects()
    buttons = []
    for p in projs:
        prefix = "● " if p == active else "📁 "
        buttons.append([InlineKeyboardButton(text=f"{prefix}{p}", callback_data=f"switch_proj:{p}")])

    # Добавляем список последних сессий для быстрого переключения
    recent_sessions = await db.list_recent_sessions(limit=5)
    if recent_sessions:
        buttons.append([InlineKeyboardButton(text="── Недавние сессии ──", callback_data="noop")])
        for s in recent_sessions:
            mark = "▶️ " if s["is_active"] and s["project_name"] == active else "💬 "
            short_prompt = (s["first_prompt"][:24] + "...") if len(s["first_prompt"]) > 24 else s["first_prompt"]
            btn_title = f"{mark}#{s['id']} [{s['project_name']}] {short_prompt}"
            buttons.append([InlineKeyboardButton(text=btn_title, callback_data=f"switch_sess:{s['id']}")])

    buttons.append([
        InlineKeyboardButton(text="✨ Новая сессия", callback_data="menu:new_session_dialog"),
        InlineKeyboardButton(text="➕ Создать/клонировать", switch_inline_query_current_chat="new "),
    ])
    buttons.append([InlineKeyboardButton(text="« Главное меню", callback_data="menu:start")])

    await message.answer(
        f"<b>▪ Управление проектами и сессиями</b>\n"
        f"• Активный проект: <code>{active}</code>\n"
        f"• Рабочая директория: <code>workspaces/{active}</code>\n\n"
        f"Выберите проект или сессию, чтобы мгновенно переключить папку агента:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("preset_url:"))
async def cb_preset_url(call: CallbackQuery, state: FSMContext):
    url = call.data.split("preset_url:", 1)[1]
    await db.set_setting("llm_base_url", url)
    await state.set_state(SetupWizard.waiting_llm_key)
    await call.message.edit_text(
        f"• Base URL: <code>{url}</code>\n\n"
        f"Шаг 2/5 • Отправьте ваш <b>LLM API Key</b>:",
        parse_mode="HTML",
    )


@dp.message(SetupWizard.waiting_llm_url)
async def step_llm_url(message: Message, state: FSMContext):
    url = message.text.strip().rstrip("/")
    await db.set_setting("llm_base_url", url)
    await state.set_state(SetupWizard.waiting_llm_key)
    await message.answer(
        f"• Base URL: <code>{url}</code>\n\n"
        f"Шаг 2/5 • Отправьте ваш <b>LLM API Key</b>:",
        parse_mode="HTML",
    )


@dp.message(SetupWizard.waiting_llm_key)
async def step_llm_key(message: Message, state: FSMContext):
    key = message.text.strip()
    url = await db.get_setting("llm_base_url", "https://openrouter.ai/api/v1")
    await db.set_setting("llm_api_key", key)
    await db.add_provider("default", url, key)
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(SetupWizard.waiting_tavily_key)
    await message.answer(
        "• LLM API Key сохранен.\n\n"
        "Шаг 3/5 • Отправьте <b>Tavily API Key</b> (<code>tvly-...</code>) для веб-поиска:",
        parse_mode="HTML",
        reply_markup=get_skip_kb("tavily"),
    )


@dp.message(SetupWizard.waiting_tavily_key)
async def step_tavily_key(message: Message, state: FSMContext):
    await db.set_setting("tavily_api_key", message.text.strip())
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(SetupWizard.waiting_groq_key)
    await message.answer(
        "• Tavily API Key сохранен.\n\n"
        "Шаг 4/5 • Отправьте <b>Groq API Key</b> (<code>gsk_...</code>) для распознавания голоса (Whisper):",
        parse_mode="HTML",
        reply_markup=get_skip_kb("groq"),
    )


@dp.message(SetupWizard.waiting_groq_key)
async def step_groq_key(message: Message, state: FSMContext):
    await db.set_setting("groq_api_key", message.text.strip())
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(SetupWizard.waiting_github_token)
    await message.answer(
        "• Groq API Key сохранен.\n\n"
        "Шаг 5/5 • Отправьте <b>GitHub Classic Token</b> (<code>ghp_...</code>) или пропустите шаг:",
        parse_mode="HTML",
        reply_markup=get_skip_kb("github"),
    )


@dp.message(SetupWizard.waiting_github_token)
async def step_github_token(message: Message, state: FSMContext):
    await db.set_setting("github_token", message.text.strip())
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    await workspace.ensure_project_initialized("default")
    await db.set_setting("active_project", "default")
    await cmd_start(message, state)


@dp.callback_query(F.data.startswith("skip_setup:"))
async def cb_skip_setup(call: CallbackQuery, state: FSMContext):
    step = call.data.split(":", 1)[1]
    if step == "tavily":
        await state.set_state(SetupWizard.waiting_groq_key)
        await call.message.edit_text(
            "› Tavily пропущен.\n\n"
            "Шаг 4/5 • Отправьте <b>Groq API Key</b> (<code>gsk_...</code>) для распознавания голоса:",
            parse_mode="HTML",
            reply_markup=get_skip_kb("groq"),
        )
    elif step == "groq":
        await state.set_state(SetupWizard.waiting_github_token)
        await call.message.edit_text(
            "› Groq Whisper пропущен.\n\n"
            "Шаг 5/5 • Отправьте <b>GitHub Classic Token</b> (<code>ghp_...</code>) или пропустите шаг:",
            parse_mode="HTML",
            reply_markup=get_skip_kb("github"),
        )
    elif step == "github":
        await state.clear()
        await workspace.ensure_project_initialized("default")
        await db.set_setting("active_project", "default")
        await call.message.delete()
        await cmd_start(call.message, state)


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("menu:"))
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    action = call.data.split("menu:", 1)[1]
    if action == "start":
        await call.message.delete()
        await cmd_start(call.message, state)
    elif action == "new_session_dialog":
        await call.message.edit_text(
            "<b>▪ Создание / смена сессии</b>\n\n"
            "Выберите режим создания новой чистой сессии (каждая сессия изолирована в своей отдельной папке!):",
            parse_mode="HTML",
            reply_markup=get_new_session_kb(),
        )
    elif action == "projects":
        await call.message.delete()
        await cmd_projects(call.message)
    elif action == "config":
        await call.message.delete()
        await cmd_config(call.message)
    elif action == "reconfigure":
        await call.message.delete()
        await start_onboarding(call.message, state)
    elif action == "init_guide":
        await cmd_init(call.message)
        await call.answer("CLAUDE.md & AGENT.md проверены")
    elif action == "stop":
        task = ACTIVE_TASKS.get(call.message.chat.id)
        if task and not task.done():
            task.cancel()
            await call.answer("Генерация прервана", show_alert=True)
        else:
            await call.answer("Нет активных задач", show_alert=True)


@dp.callback_query(F.data.startswith("sess_create:"))
async def cb_sess_create(call: CallbackQuery):
    mode = call.data.split("sess_create:", 1)[1]
    if mode == "local":
        proj_slug = f"session_{int(time.time()) % 100000}"
        await workspace.ensure_project_initialized(proj_slug)
        await db.set_setting("active_project", proj_slug)
        sid = await db.start_new_session(proj_slug)
        await call.answer("Создана чистая сессия")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧠 Сменить модель", switch_inline_query_current_chat="")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="menu:start")],
            ]
        )
        await call.message.edit_text(
            f"<b>✨ Чистая локальная сессия #{sid} создана!</b>\n"
            f"<code>────────────────────────</code>\n"
            f"• Отдельная папка проекта: <code>workspaces/{proj_slug}</code>\n"
            f"• Git-репозиторий и память инициализированы.\n"
            f"• Предыдущие проекты сохранены в сохранности.\n\n"
            f"<i>Отправьте любую задачу или сообщение голосом.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )


@dp.callback_query(F.data.startswith("agent_action:"))
async def cb_agent_action(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    project_name = await db.get_setting("active_project", "default")
    session_id = await db.get_or_create_active_session(project_name)

    if action == "rollback":
        last_turn = await db.pop_last_turn(session_id)
        if not last_turn:
            await call.answer("История сессии пуста.", show_alert=True)
            return
        _, msg = await workspace.rollback_to_checkpoint(project_name, last_turn["pre_commit_sha"])
        await call.answer("Откат выполнен")
        await call.message.edit_text(
            call.message.html_text + f"\n\n<code>↶ {html.escape(msg)}</code>",
            parse_mode="HTML",
        )

    elif action == "retry":
        last_turn = await db.pop_last_turn(session_id)
        if not last_turn:
            await call.answer("Нет шага для повтора.", show_alert=True)
            return
        await workspace.rollback_to_checkpoint(project_name, last_turn["pre_commit_sha"])
        await call.answer("Повторяю шаг...")
        await execute_user_prompt(call.bot, call.message.chat.id, last_turn["user_prompt"])

    elif action == "new_session":
        # Создаем новую чистую изолированную сессию
        proj_slug = f"session_{int(time.time()) % 100000}"
        await workspace.ensure_project_initialized(proj_slug)
        await db.set_setting("active_project", proj_slug)
        new_sid = await db.start_new_session(proj_slug)
        await call.answer("Новая сессия")
        await call.message.answer(
            f"<b>+ Новая сессия #{new_sid}</b>\n"
            f"• Отдельная изолированная папка: <code>workspaces/{proj_slug}</code>\n"
            f"• Контекст сброшен, файлы предыдущих сессий в безопасности.",
            parse_mode="HTML",
        )


@dp.callback_query(F.data.startswith("switch_proj:"))
async def cb_switch_proj(call: CallbackQuery):
    proj = call.data.split(":", 1)[1]
    await workspace.ensure_project_initialized(proj)
    await db.set_setting("active_project", proj)
    sid = await db.get_or_create_active_session(proj)
    await call.answer(f"Открыт проект: {proj}")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Новая сессия", callback_data="menu:new_session_dialog")],
            [InlineKeyboardButton(text="« Назад к списку", callback_data="menu:projects")],
        ]
    )
    await call.message.edit_text(
        f"<b>▪ Активный проект переключен:</b> <code>{proj}</code>\n"
        f"• Рабочая папка агента: <code>workspaces/{proj}</code>\n"
        f"• Активная сессия: #{sid}\n"
        f"Агент теперь работает исключительно в этой папке.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("switch_sess:"))
async def cb_switch_sess(call: CallbackQuery):
    sess_id = int(call.data.split(":", 1)[1])
    proj = await db.switch_to_session(sess_id)
    if proj:
        await db.set_setting("active_project", proj)
        await call.answer(f"Сессия #{sess_id} активирована")
        await call.message.edit_text(
            f"<b>▪ Переключено на сессию #{sess_id}</b>\n"
            f"• Проект: <code>{proj}</code>\n"
            f"• Папка: <code>workspaces/{proj}</code>\n"
            f"Вся история диалога и файлы этой сессии снова активны у агента.",
            parse_mode="HTML",
        )
    else:
        await call.answer("Сессия не найдена", show_alert=True)


@dp.callback_query(F.data.startswith("clone_with_branch:"))
async def cb_clone_with_branch(call: CallbackQuery):
    # data: clone_with_branch:full_repo:branch
    _, repo, branch = call.data.split(":", 2)
    await call.message.edit_text(f"<i>клонирую {html.escape(repo)} (ветка {html.escape(branch)})...</i>", parse_mode="HTML")
    gh_token = await db.get_setting("github_token", "")
    ok, msg = await workspace.clone_github_repo(gh_token, repo, branch=branch)
    if ok:
        slug = sanitize_project_name(repo.split("/")[-1])
        await db.set_setting("active_project", slug)
        sid = await db.start_new_session(slug)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="« В главное меню", callback_data="menu:start")],
            ]
        )
        await call.message.edit_text(
            f"<b>▪ Репозиторий успешно подключен!</b>\n"
            f"• Репо: <code>{html.escape(repo)}</code> (ветка <code>{html.escape(branch)}</code>)\n"
            f"• Папка проекта: <code>workspaces/{slug}</code>\n"
            f"• Новая сессия: #{sid}\n\n"
            f"<i>Агент готов к работе с проектом.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await call.message.edit_text(f"<b>[ошибка клонирования]</b>\n{html.escape(msg)}", parse_mode="HTML")


@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    raw_q = inline_query.query.strip()
    results: List[InlineQueryResultArticle] = []

    if raw_q.lower().startswith("api "):
        parts = raw_q.split()
        if len(parts) >= 3:
            b_url, a_key = parts[1], parts[2]
            p_name = parts[3] if len(parts) >= 4 else "custom"
            results.append(
                InlineQueryResultArticle(
                    id="add_api_now",
                    title=f"+ Подключить API: {p_name} ({b_url})",
                    description="Сохранить ключ и переключиться на этот эндпоинт",
                    input_message_content=InputTextMessageContent(
                        message_text=f"/cmd_add_api {p_name} {b_url} {a_key}"
                    ),
                )
            )
        else:
            results.append(
                InlineQueryResultArticle(
                    id="add_api_help",
                    title="Формат: @bot api <base_url> <api_key> [имя]",
                    description="Пример: @bot api https://api.deepseek.com/v1 sk-xxxx deepseek",
                    input_message_content=InputTextMessageContent(
                        message_text="Формат: @bot api https://... sk-..."
                    ),
                )
            )
        await inline_query.answer(results, cache_time=1, is_personal=True)
        return

    if raw_q.lower().startswith("new"):
        proj_name = sanitize_project_name(raw_q[3:].strip() or f"proj_{int(time.time()) % 10000}")
        results.append(
            InlineQueryResultArticle(
                id=f"new_local_{proj_name}",
                title=f"+ Пустой проект: {proj_name}",
                description=f"Создать папку workspaces/{proj_name}, память и локальный Git",
                input_message_content=InputTextMessageContent(message_text=f"/cmd_new_project {proj_name}"),
            )
        )
        results.append(
            InlineQueryResultArticle(
                id=f"new_gh_{proj_name}",
                title=f"+ Приватный GitHub-репо: {proj_name}",
                description="Создать репозиторий на GitHub, склонировать и открыть сессию",
                input_message_content=InputTextMessageContent(message_text=f"/cmd_new_gh_repo {proj_name} private"),
            )
        )
        await inline_query.answer(results, cache_time=1, is_personal=True)
        return

    if raw_q.lower().startswith("repo"):
        search_term = raw_q[4:].strip()
        gh_token = await db.get_setting("github_token", "")
        repos = await workspace.list_github_repos(gh_token, search_term)
        for idx, r in enumerate(repos):
            vis = "[priv]" if r["private"] else "[pub]"
            results.append(
                InlineQueryResultArticle(
                    id=f"gh_repo_{idx}",
                    title=f"▸ {r['full_name']} {vis}",
                    description=f"Выбрать ветку и открыть проект • {r['description'][:70]}",
                    input_message_content=InputTextMessageContent(
                        message_text=f"/cmd_select_repo {r['full_name']}"
                    ),
                )
            )
        if "/" in search_term:
            results.insert(
                0,
                InlineQueryResultArticle(
                    id="gh_clone_direct",
                    title=f"▸ {search_term} (прямой ввод)",
                    description="Выбрать репозиторий для настройки и клонирования",
                    input_message_content=InputTextMessageContent(
                        message_text=f"/cmd_select_repo {search_term}"
                    ),
                ),
            )
        if not results:
            results.append(
                InlineQueryResultArticle(
                    id="no_repos",
                    title="▪ Репозитории не найдены",
                    description="Введите @bot repo owner/repo или настройте GitHub токен в /settings",
                    input_message_content=InputTextMessageContent(message_text="/session"),
                )
            )
        await inline_query.answer(results[:30], cache_time=2, is_personal=True)
        return

    models = await search_models_online(raw_q)
    active_cfg = await db.get_active_llm_config()

    for idx, m in enumerate(models):
        is_current = m["model_id"] == active_cfg["model"]
        mark = "● " if is_current else "▸ "
        ctx_k = int(m.get("context_length", 128000)) // 1000
        results.append(
            InlineQueryResultArticle(
                id=f"model_{idx}",
                title=f"{mark}{m['model_id']}",
                description=f"контекст: {ctx_k}k • провайдер: {m.get('provider', 'default')}",
                input_message_content=InputTextMessageContent(
                    message_text=f"/cmd_set_model {m['model_id']}"
                ),
            )
        )

    if raw_q and not any(m["model_id"].lower() == raw_q.lower() for m in models):
        results.insert(
            0,
            InlineQueryResultArticle(
                id="custom_model_add",
                title=f"+ Добавить и выбрать модель: {raw_q}",
                description="Сохранить ID модели и сделать активной",
                input_message_content=InputTextMessageContent(
                    message_text=f"/cmd_set_model {raw_q}"
                ),
            ),
        )

    await inline_query.answer(results[:35], cache_time=2, is_personal=True)


@dp.message(F.text.startswith("/cmd_"))
async def handle_inline_service_commands(message: Message):
    text = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    if text.startswith("/cmd_set_model "):
        model_id = text.split("/cmd_set_model ", 1)[1].strip()
        await db.save_model(model_id)
        await db.set_setting("active_model", model_id)
        await message.answer(f"<b>▪ Модель:</b> <code>{html.escape(model_id)}</code>", parse_mode="HTML")

    elif text.startswith("/cmd_add_api "):
        parts = text.split()
        if len(parts) >= 4:
            p_name, b_url, a_key = parts[1], parts[2], parts[3]
            await db.add_provider(p_name, b_url, a_key)
            await db.set_setting("active_provider", p_name)
            await db.set_setting("llm_base_url", b_url)
            await db.set_setting("llm_api_key", a_key)
            ONLINE_MODELS_CACHE["ts"] = 0
            await message.answer(
                f"<b>▪ API подключен:</b> <code>{html.escape(p_name)}</code> (<code>{html.escape(b_url)}</code>)",
                parse_mode="HTML",
            )

    elif text.startswith("/cmd_new_project "):
        proj_name = sanitize_project_name(text.split("/cmd_new_project ", 1)[1])
        await workspace.ensure_project_initialized(proj_name)
        await db.set_setting("active_project", proj_name)
        await db.start_new_session(proj_name)
        await message.answer(
            f"<b>+ Проект создан:</b> <code>{proj_name}</code>\n"
            f"• папка: <code>workspaces/{proj_name}</code>",
            parse_mode="HTML",
        )

    elif text.startswith("/cmd_new_gh_repo "):
        parts = text.split()
        proj_name = sanitize_project_name(parts[1])
        is_priv = (parts[2].lower() == "private") if len(parts) > 2 else True
        status_msg = await message.answer(f"<i>создаю репозиторий {proj_name}...</i>", parse_mode="HTML")
        gh_token = await db.get_setting("github_token", "")
        ok, msg, actual_name = await workspace.create_github_repo(gh_token, proj_name, private=is_priv)
        if ok and actual_name:
            await db.set_setting("active_project", actual_name)
            await db.start_new_session(actual_name)
        await status_msg.edit_text(f"<b>{'▪' if ok else '[x]'}</b> {md_to_tg_html(msg)}", parse_mode="HTML")

    elif text.startswith("/cmd_select_repo "):
        full_repo = text.split("/cmd_select_repo ", 1)[1].strip()
        status_msg = await message.answer(f"<i>получаю ветки репозитория {html.escape(full_repo)}...</i>", parse_mode="HTML")
        gh_token = await db.get_setting("github_token", "")
        branches = await workspace.list_github_branches(gh_token, full_repo)

        buttons = []
        for b in branches[:8]:
            buttons.append([InlineKeyboardButton(text=f"🌿 Ветка: {b}", callback_data=f"clone_with_branch:{full_repo}:{b}")])
        buttons.append([InlineKeyboardButton(text="« Отмена", callback_data="menu:start")])

        await status_msg.edit_text(
            f"<b>🐙 Выберите ветку для репозитория:</b>\n"
            f"<code>{html.escape(full_repo)}</code>\n\n"
            f"После выбора агент создаст отдельную сессию и папку проекта:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    elif text.startswith("/cmd_clone_repo "):
        full_repo = text.split("/cmd_clone_repo ", 1)[1].strip()
        status_msg = await message.answer(f"<i>клонирую {html.escape(full_repo)}...</i>", parse_mode="HTML")
        gh_token = await db.get_setting("github_token", "")
        ok, msg = await workspace.clone_github_repo(gh_token, full_repo)
        if ok:
            slug = sanitize_project_name(full_repo.split("/")[-1])
            await db.set_setting("active_project", slug)
            await db.start_new_session(slug)
        await status_msg.edit_text(f"<b>{'▪' if ok else '[x]'}</b> {md_to_tg_html(msg)}", parse_mode="HTML")


@dp.message(F.voice | F.audio)
async def handle_voice_message(message: Message, state: FSMContext):
    if not await db.is_configured():
        await start_onboarding(message, state)
        return

    file_id = message.voice.file_id if message.voice else message.audio.file_id
    status = await message.answer("<i>распознаю аудио...</i>", parse_mode="HTML")
    try:
        text = await transcribe_voice_groq(message.bot, file_id)
        if not text:
            await status.edit_text("▪ Пустой аудио-запрос.")
            return
        await status.edit_text(f"<b>› голос:</b> <i>{html.escape(text)}</i>", parse_mode="HTML")
        await execute_user_prompt(message.bot, message.chat.id, text)
    except Exception as e:
        await status.edit_text(f"<b>[ошибка аудио]</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@dp.message(F.text)
async def handle_text_task(message: Message, state: FSMContext):
    if not await db.is_configured():
        await start_onboarding(message, state)
        return

    prompt = message.text.strip()
    if not prompt:
        return
    await execute_user_prompt(message.bot, message.chat.id, prompt)


async def main():
    init_default_skills()
    await db.init()
    await workspace.ensure_project_initialized("default")

    token = get_bot_token()
    bot = Bot(token=token)
    await setup_bot_commands(bot)

    me = await bot.get_me()
    print(f"\n▪ Agent @{me.username} запущен (Admin ID: {ADMIN_ID})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
