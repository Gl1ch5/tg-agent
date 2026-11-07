import json
import time
import aiosqlite
from typing import Optional, Dict, Any, List
from config import DB_PATH


class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS api_providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    base_url TEXT,
                    api_key TEXT
                );

                CREATE TABLE IF NOT EXISTS models (
                    model_id TEXT PRIMARY KEY,
                    provider_name TEXT,
                    display_name TEXT,
                    context_length INTEGER DEFAULT 128000
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT,
                    created_at REAL,
                    is_active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    user_prompt TEXT,
                    assistant_report TEXT,
                    messages_json TEXT,
                    pre_commit_sha TEXT,
                    post_commit_sha TEXT,
                    tokens_used INTEGER DEFAULT 0,
                    elapsed_sec REAL DEFAULT 0.0,
                    created_at REAL
                );
                """
            )
            await db.commit()

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def is_configured(self) -> bool:
        llm_key = await self.get_setting("llm_api_key")
        llm_url = await self.get_setting("llm_base_url")
        return bool(llm_key and llm_url)

    async def add_provider(self, name: str, base_url: str, api_key: str):
        base_url = base_url.rstrip("/")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO api_providers (name, base_url, api_key) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_url = excluded.base_url, api_key = excluded.api_key",
                (name, base_url, api_key),
            )
            await db.commit()

    async def get_provider(self, name: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name, base_url, api_key FROM api_providers WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {"name": row[0], "base_url": row[1], "api_key": row[2]}
                return None

    async def get_active_llm_config(self) -> Dict[str, str]:
        model_id = await self.get_setting("active_model", "anthropic/claude-3.5-sonnet")
        provider_name = await self.get_setting("active_provider", "default")
        prov = await self.get_provider(provider_name)
        if prov:
            return {
                "model": model_id,
                "base_url": prov["base_url"],
                "api_key": prov["api_key"],
                "provider": provider_name,
            }
        return {
            "model": model_id,
            "base_url": (await self.get_setting("llm_base_url", "https://openrouter.ai/api/v1")).rstrip("/"),
            "api_key": await self.get_setting("llm_api_key", ""),
            "provider": "default",
        }

    async def save_model(self, model_id: str, provider_name: str = "default", display_name: str = "", context_length: int = 128000):
        if not display_name:
            display_name = model_id
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO models (model_id, provider_name, display_name, context_length) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(model_id) DO UPDATE SET provider_name = excluded.provider_name, display_name = excluded.display_name",
                (model_id, provider_name, display_name, context_length),
            )
            await db.commit()

    async def list_saved_models(self, query: str = "") -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            if query:
                q = f"%{query.lower()}%"
                sql = "SELECT model_id, provider_name, display_name, context_length FROM models WHERE LOWER(model_id) LIKE ? OR LOWER(display_name) LIKE ? LIMIT 30"
                params = (q, q)
            else:
                sql = "SELECT model_id, provider_name, display_name, context_length FROM models LIMIT 30"
                params = ()
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [
                    {"model_id": r[0], "provider": r[1], "display_name": r[2], "context_length": r[3]}
                    for r in rows
                ]

    async def list_recent_sessions(self, limit: int = 15) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT s.id, s.project_name, s.created_at, s.is_active,
                       (SELECT user_prompt FROM turns WHERE session_id = s.id ORDER BY id ASC LIMIT 1) as first_prompt,
                       (SELECT COUNT(*) FROM turns WHERE session_id = s.id) as turn_count
                FROM sessions s
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "project_name": r[1],
                        "created_at": r[2],
                        "is_active": bool(r[3]),
                        "first_prompt": r[4] or "Пустая сессия",
                        "turn_count": r[5] or 0,
                    }
                    for r in rows
                ]

    async def switch_to_session(self, session_id: int) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT project_name FROM sessions WHERE id = ?", (session_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                proj_name = row[0]
            await db.execute("UPDATE sessions SET is_active = 0 WHERE project_name = ?", (proj_name,))
            await db.execute("UPDATE sessions SET is_active = 1 WHERE id = ?", (session_id,))
            await db.commit()
            return proj_name

    async def get_or_create_active_session(self, project_name: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM sessions WHERE project_name = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
                (project_name,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]
            cur = await db.execute(
                "INSERT INTO sessions (project_name, created_at, is_active) VALUES (?, ?, 1)",
                (project_name, time.time()),
            )
            await db.commit()
            return cur.lastrowid

    async def start_new_session(self, project_name: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET is_active = 0 WHERE project_name = ?",
                (project_name,),
            )
            cur = await db.execute(
                "INSERT INTO sessions (project_name, created_at, is_active) VALUES (?, ?, 1)",
                (project_name, time.time()),
            )
            await db.commit()
            return cur.lastrowid

    async def add_turn(
        self,
        session_id: int,
        user_prompt: str,
        assistant_report: str,
        messages_list: List[Dict[str, Any]],
        pre_commit_sha: str,
        post_commit_sha: str,
        tokens_used: int,
        elapsed_sec: float,
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO turns (
                    session_id, user_prompt, assistant_report, messages_json,
                    pre_commit_sha, post_commit_sha, tokens_used, elapsed_sec, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_prompt,
                    assistant_report,
                    json.dumps(messages_list, ensure_ascii=False),
                    pre_commit_sha,
                    post_commit_sha,
                    tokens_used,
                    elapsed_sec,
                    time.time(),
                ),
            )
            await db.commit()
            return cur.lastrowid

    async def get_session_turns(self, session_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, user_prompt, assistant_report, messages_json, pre_commit_sha, post_commit_sha, tokens_used FROM turns WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "user_prompt": r[1],
                        "assistant_report": r[2],
                        "messages": json.loads(r[3]),
                        "pre_commit_sha": r[4],
                        "post_commit_sha": r[5],
                        "tokens_used": r[6],
                    }
                    for r in rows
                ]

    async def pop_last_turn(self, session_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, user_prompt, pre_commit_sha, post_commit_sha FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                turn_data = {
                    "id": row[0],
                    "user_prompt": row[1],
                    "pre_commit_sha": row[2],
                    "post_commit_sha": row[3],
                }
            await db.execute("DELETE FROM turns WHERE id = ?", (turn_data["id"],))
            await db.commit()
            return turn_data


db = Database()
