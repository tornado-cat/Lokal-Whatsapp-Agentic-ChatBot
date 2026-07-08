from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from app.schemas import SessionState


class SessionStore(Protocol):
    async def get(self, session_key: str) -> SessionState | None:
        ...

    async def set(self, session_key: str, session: SessionState) -> None:
        ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    async def get(self, session_key: str) -> SessionState | None:
        return self._sessions.get(session_key)

    async def set(self, session_key: str, session: SessionState) -> None:
        self._sessions[session_key] = session


class SessionManager:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or InMemorySessionStore()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def build_session_key(tenant_id: str, user_id: str) -> str:
        return f"{tenant_id}:{user_id}"

    async def get_or_create(self, tenant_id: str, phone: str) -> tuple[str, SessionState]:
        session_key = self.build_session_key(tenant_id, phone)
        session = await self.store.get(session_key)
        if session is None:
            session = SessionState(tenant_id=tenant_id, phone=phone)
            await self.store.set(session_key, session)
        session.updated_at = datetime.utcnow()
        return session_key, session

    async def get_lock(self, session_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            if session_key not in self._locks:
                self._locks[session_key] = asyncio.Lock()
            return self._locks[session_key]
