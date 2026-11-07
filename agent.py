import json
import time
from typing import List, Dict, Any, Callable, Awaitable, Tuple, Optional
import httpx
from db import db
from workspace import workspace
from tools import TOOLS_SCHEMA, execute_tool
from formatter import strip_emojis


def build_economical_system_prompt(project_name: str) -> str:
    project_dir = workspace.get_project_dir(project_name)
    files_preview = []
    for item in sorted(project_dir.iterdir())[:30]:
        if item.name.startswith(".git"):
            continue
        suffix = "/" if item.is_dir() else ""
        files_preview.append(f"{item.name}{suffix}")

    memory_text = workspace.read_memory(project_name)
    skills_list = workspace.list_skills_compact()

    guide_text = ""
    for guide_name in ("AGENT.md", "CLAUDE.md", "agent.md", "claude.md"):
        g_file = project_dir / guide_name
        if g_file.exists():
            content = g_file.read_text(encoding="utf-8", errors="replace")[:4000]
            guide_text += f"\n\nИнструкция проекта ({guide_name}):\n{content}"

    return (
        f"Ты — автономный ИИ-агент разработчика.\n"
        f"Проект: `{project_name}` (рабочая папка: `{project_dir}`).\n"
        f"Файлы в корне: {', '.join(files_preview) if files_preview else 'пусто'}.\n\n"
        f"Память проекта (.agent_memory.md):\n{memory_text or 'Пусто.'}"
        f"{guide_text}\n\n"
        f"Доступные скиллы (вызови `load_skill` при необходимости):\n{skills_list}\n\n"
        f"Правила работы и стиля:\n"
        f"1. Действуй по циклу ReAct: используй инструменты (`bash`, `read_file`, `edit_file`, `write_file`, `web_search`, `github_action`, `send_media`), проверяй результат.\n"
        f"2. Экономь токены: не читай большие файлы целиком, используй диапазоны строк или `grep -n`.\n"
        f"3. СТРОГОЕ ОФОРМЛЕНИЕ: НИКОГДА не используй эмодзи (смайлики, иконки). Пиши чистым техническим языком, используй короткие абзацы, списки и блоки кода."
    )


def compress_history_for_economy(turns: List[Dict[str, Any]], max_recent_turns: int = 6) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    older = turns[:-max_recent_turns] if len(turns) > max_recent_turns else []
    recent = turns[-max_recent_turns:] if len(turns) > max_recent_turns else turns

    for t in older:
        history.append({"role": "user", "content": t["user_prompt"][:600]})
        history.append({"role": "assistant", "content": t["assistant_report"][:1000]})

    for t in recent:
        for msg in t.get("messages", []):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if len(content) > 900:
                    msg = dict(msg)
                    msg["content"] = content[:450] + "\n...[сжато]...\n" + content[-300:]
            history.append(msg)

    return history


async def stream_llm_step(
    llm_cfg: Dict[str, str],
    messages: List[Dict[str, Any]],
    on_token_chunk: Callable[[str], Awaitable[None]],
) -> Tuple[str, List[Dict[str, Any]], int]:
    url = f"{llm_cfg['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm_cfg['api_key']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/",
        "X-Title": "Telegram AI Agent",
    }
    payload = {
        "model": llm_cfg["model"],
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    content_parts: List[str] = []
    tool_calls_acc: Dict[int, Dict[str, Any]] = {}
    tokens_used = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                err_body = await resp.aread()
                if resp.status_code == 400 and "stream_options" in err_body.decode("utf-8", errors="ignore"):
                    payload.pop("stream_options", None)
                else:
                    raise RuntimeError(f"LLM API Error ({resp.status_code}): {err_body.decode('utf-8', errors='replace')[:400]}")

            if "stream_options" not in payload:
                resp2 = await client.post(url, headers=headers, json={**payload, "stream": False})
                data = resp2.json()
                choice = data["choices"][0]["message"]
                text = choice.get("content") or ""
                if text:
                    await on_token_chunk(text)
                tcalls = choice.get("tool_calls") or []
                usage = data.get("usage", {}).get("total_tokens", 0)
                return text, tcalls, usage

            async for raw_line in resp.aiter_lines():
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data_str = raw_line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue

                if chunk.get("usage"):
                    tokens_used = chunk["usage"].get("total_tokens", tokens_used)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                if delta.get("content"):
                    piece = delta["content"]
                    content_parts.append(piece)
                    await on_token_chunk("".join(content_parts))

                if delta.get("tool_calls"):
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.get("id") or f"call_{idx}_{int(time.time())}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            tool_calls_acc[idx]["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tool_calls_acc[idx]["function"]["arguments"] += fn["arguments"]

    full_text = "".join(content_parts)
    tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
    if not tokens_used:
        tokens_used = (sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) + len(full_text)) // 4

    return full_text, tool_calls_list, tokens_used


async def run_react_agent(
    project_name: str,
    session_id: int,
    user_prompt: str,
    on_progress: Callable[[List[str], str, float, int], Awaitable[None]],
    max_steps: Optional[int] = None,
) -> Dict[str, Any]:
    start_ts = time.time()
    llm_cfg = await db.get_active_llm_config()
    pre_sha = await workspace.create_checkpoint(project_name, f"Before: {user_prompt[:40]}")

    past_turns = await db.get_session_turns(session_id)
    history_messages = compress_history_for_economy(past_turns)

    system_prompt = build_economical_system_prompt(project_name)
    turn_messages: List[Dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    steps_log: List[str] = []
    media_queue: List[Dict[str, str]] = []
    total_tokens = 0
    final_report = ""

    step_count = 0
    while True:
        step_count += 1
        if max_steps is not None and step_count > max_steps:
            final_report = text_out.strip() or f"Достигнут лимит шагов агента ({max_steps}). Промежуточные изменения сохранены."
            turn_messages.append({"role": "assistant", "content": final_report})
            break
        full_messages = [{"role": "system", "content": system_prompt}] + history_messages + turn_messages

        async def _on_chunk(partial_text: str):
            await on_progress(steps_log, partial_text, time.time() - start_ts, total_tokens)

        text_out, tool_calls, step_tokens = await stream_llm_step(llm_cfg, full_messages, _on_chunk)
        total_tokens += step_tokens

        if not tool_calls:
            final_report = text_out.strip() or "Готово."
            turn_messages.append({"role": "assistant", "content": final_report})
            break

        assistant_msg: Dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
        if text_out.strip():
            clean_thought = strip_emojis(text_out.strip()).replace("\n", " ")
            assistant_msg["content"] = text_out.strip()
            steps_log.append(f"› {clean_thought[:120]}")
        turn_messages.append(assistant_msg)

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                parsed_args = json.loads(raw_args) if raw_args else {}
            except Exception:
                parsed_args = {"command": raw_args} if fn_name == "bash" else {}

            arg_preview = ""
            if fn_name == "bash":
                arg_preview = parsed_args.get("command", "")[:60]
            elif "path" in parsed_args:
                arg_preview = parsed_args.get("path", "")
            elif "query" in parsed_args:
                arg_preview = parsed_args.get("query", "")
            elif "action" in parsed_args:
                arg_preview = f"{parsed_args.get('action')} {parsed_args.get('repo_name', '')}"

            steps_log.append(f"▸ {fn_name}({arg_preview})")
            await on_progress(steps_log, text_out, time.time() - start_ts, total_tokens)

            observation = await execute_tool(project_name, fn_name, parsed_args, media_queue)
            turn_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": observation,
                }
            )
            await on_progress(steps_log, "", time.time() - start_ts, total_tokens)

    elapsed = time.time() - start_ts
    post_sha = await workspace.create_checkpoint(project_name, f"After: {user_prompt[:40]}")

    await db.add_turn(
        session_id=session_id,
        user_prompt=user_prompt,
        assistant_report=final_report,
        messages_list=turn_messages,
        pre_commit_sha=pre_sha,
        post_commit_sha=post_sha,
        tokens_used=total_tokens,
        elapsed_sec=elapsed,
    )

    return {
        "report": final_report,
        "steps_log": steps_log,
        "tokens": total_tokens,
        "elapsed": elapsed,
        "media": media_queue,
        "pre_sha": pre_sha,
        "post_sha": post_sha,
    }
