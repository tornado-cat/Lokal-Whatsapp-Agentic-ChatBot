from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

from app.config import Settings


def _sqlite_path_from_url(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if database_url.startswith(prefix):
        return Path(database_url.removeprefix(prefix))
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    return Path(database_url)


class ConversationRepository:
    def __init__(self, settings: Settings) -> None:
        self.db_path = _sqlite_path_from_url(settings.chatbot_database_url)
        try:
            self.timezone = ZoneInfo(settings.app_timezone)
        except ZoneInfoNotFoundError:
            self.timezone = ZoneInfo("UTC")

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    message_id TEXT,
                    user_message TEXT NOT NULL,
                    assistant_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    local_date TEXT
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id INTEGER NOT NULL,
                    session_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES conversation_turns(id)
                );

                CREATE TABLE IF NOT EXISTS user_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_key, fact_key)
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session_created
                    ON conversation_turns(session_key, created_at);

                CREATE INDEX IF NOT EXISTS idx_turns_tenant_phone
                    ON conversation_turns(tenant_id, phone);

                CREATE INDEX IF NOT EXISTS idx_tools_turn
                    ON tool_calls(turn_id);

                CREATE INDEX IF NOT EXISTS idx_facts_session
                    ON user_facts(session_key);
                """
            )
            await self._ensure_conversation_columns(db)
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_turns_session_local_date
                    ON conversation_turns(session_key, local_date)
                """
            )
            await db.commit()

    async def _ensure_conversation_columns(self, db: aiosqlite.Connection) -> None:
        rows = await db.execute_fetchall("PRAGMA table_info(conversation_turns)")
        columns = {str(row[1]) for row in rows}
        if "local_date" not in columns:
            await db.execute("ALTER TABLE conversation_turns ADD COLUMN local_date TEXT")
            await db.execute(
                """
                UPDATE conversation_turns
                SET local_date = substr(created_at, 1, 10)
                WHERE local_date IS NULL
                """
            )

    async def save_turn(
        self,
        session_key: str,
        tenant_id: str,
        phone: str,
        message_id: str | None,
        user_message: str,
        assistant_message: str,
        tools_used: list[dict[str, Any]],
    ) -> int:
        now = datetime.utcnow().isoformat()
        local_date = self.current_local_date()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO conversation_turns (
                    session_key,
                    tenant_id,
                    phone,
                    message_id,
                    user_message,
                    assistant_message,
                    created_at,
                    local_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key,
                    tenant_id,
                    phone,
                    message_id,
                    user_message,
                    assistant_message,
                    now,
                    local_date,
                ),
            )
            turn_id = int(cursor.lastrowid)

            for tool_call in tools_used:
                await db.execute(
                    """
                    INSERT INTO tool_calls (
                        turn_id,
                        session_key,
                        tool_name,
                        arguments_json,
                        result_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        session_key,
                        str(tool_call.get("name", "")),
                        json.dumps(tool_call.get("arguments", {}), ensure_ascii=False),
                        json.dumps(tool_call.get("result", {}), ensure_ascii=False),
                        now,
                    ),
                )

            await db.commit()
            return turn_id

    def current_local_date(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    async def has_turn_today(self, session_key: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchall(
                """
                SELECT 1
                FROM conversation_turns
                WHERE session_key = ? AND local_date = ?
                LIMIT 1
                """,
                (session_key, self.current_local_date()),
            )
            return bool(row)

    async def list_turns(
        self,
        session_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, session_key, tenant_id, phone, message_id,
                   user_message, assistant_message, created_at, local_date
            FROM conversation_turns
        """
        params: list[Any] = []
        if session_key:
            query += " WHERE session_key = ?"
            params.append(session_key)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(query, params)
            turns = [dict(row) for row in rows]

            for turn in turns:
                tool_rows = await db.execute_fetchall(
                    """
                    SELECT tool_name, arguments_json, result_json, created_at
                    FROM tool_calls
                    WHERE turn_id = ?
                    ORDER BY id ASC
                    """,
                    (turn["id"],),
                )
                turn["tools_used"] = [
                    {
                        "name": row["tool_name"],
                        "arguments": json.loads(row["arguments_json"]),
                        "result": json.loads(row["result_json"]),
                        "created_at": row["created_at"],
                    }
                    for row in tool_rows
                ]

            return turns

    async def get_recent_turns(
        self,
        session_key: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT user_message, assistant_message, created_at
                FROM conversation_turns
                WHERE session_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_key, limit),
            )
            return [dict(row) for row in reversed(rows)]

    async def upsert_fact(
        self,
        session_key: str,
        tenant_id: str,
        phone: str,
        fact_key: str,
        fact_value: str,
    ) -> None:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_facts (
                    session_key,
                    tenant_id,
                    phone,
                    fact_key,
                    fact_value,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key, fact_key)
                DO UPDATE SET
                    fact_value = excluded.fact_value,
                    updated_at = excluded.updated_at
                """,
                (session_key, tenant_id, phone, fact_key, fact_value, now),
            )
            await db.commit()

    async def get_facts(self, session_key: str) -> dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT fact_key, fact_value
                FROM user_facts
                WHERE session_key = ?
                """,
                (session_key,),
            )
            return {str(row["fact_key"]): str(row["fact_value"]) for row in rows}

    async def list_facts(
        self,
        session_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT session_key, tenant_id, phone, fact_key, fact_value, updated_at
            FROM user_facts
        """
        params: list[Any] = []
        if session_key:
            query += " WHERE session_key = ?"
            params.append(session_key)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(query, params)
            return [dict(row) for row in rows]
