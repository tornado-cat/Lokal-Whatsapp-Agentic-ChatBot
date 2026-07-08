from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.agent.memory import ConversationMemory
from app.agent.tools import TOOL_DEFINITIONS, run_tool
from app.config import Settings
from app.schemas import SessionState

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
Sen localhost üzerinde çalışan multi-tenant bir WhatsApp asistanısın.
Kısa, net ve doğal Türkçe yanıt ver.
Profil veya hesap sorularında get_user_profile tool'unu kullan.
Sunucu, sistem, RAM, disk, CPU veya performans sorularında check_local_system_status tool'unu kullan.
Tool sonucu geldiyse sonucu kullanıcı dostu şekilde özetle.
"""


class AgentService:
    def __init__(self, settings: Settings, memory: ConversationMemory) -> None:
        self.settings = settings
        self.memory = memory
        self.client = self._build_client(settings)
        self.system_prompt = self._load_system_prompt(settings)

    @staticmethod
    def _build_client(settings: Settings) -> AsyncOpenAI | None:
        if not settings.openai_api_key and not settings.llm_base_url:
            return None
        return AsyncOpenAI(
            api_key=settings.openai_api_key or "local-dev-key",
            base_url=settings.llm_base_url or None,
        )

    @staticmethod
    def _load_system_prompt(settings: Settings) -> str:
        prompt_path = Path(settings.agent_system_prompt_path)
        try:
            if prompt_path.exists():
                content = prompt_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except OSError:
            logger.exception("Could not read system prompt path=%s", prompt_path)
        return SYSTEM_PROMPT.strip()

    async def generate_reply(self, session: SessionState) -> str:
        if self.client is None:
            return self._generate_fallback_reply(session)

        try:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                *self.memory.to_openai_messages(session),
            ]
            first = await self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                max_tokens=512,
                extra_body={"options": {"num_predict": 512}},
            )
            assistant_message = first.choices[0].message
            tool_calls = assistant_message.tool_calls or []

            if not tool_calls:
                return assistant_message.content or "Şu anda kısa bir yanıt üretemedim."

            messages.append(assistant_message.model_dump(exclude_none=True))
            for tool_call in tool_calls:
                args = json.loads(tool_call.function.arguments or "{}")
                result = run_tool(tool_call.function.name, args, session.phone)
                result_text = json.dumps(result, ensure_ascii=False)
                self.memory.add_message(
                    session,
                    role="tool",
                    content=result_text,
                    tool_name=tool_call.function.name,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": result_text,
                    }
                )

            second = await self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                max_tokens=512,
                extra_body={"options": {"num_predict": 512}},
            )
            return second.choices[0].message.content or "İşlemi tamamladım."
        except Exception:
            logger.exception("LLM generation failed, using local fallback")
            return self._generate_fallback_reply(session)

    def _generate_fallback_reply(self, session: SessionState) -> str:
        last_user_message = next(
            (item.content for item in reversed(session.memory) if item.role == "user"),
            "",
        )
        normalized = last_user_message.lower()

        if any(word in normalized for word in ["profil", "hesap", "plan", "ben kimim"]):
            result = run_tool("get_user_profile", {"phone": session.phone}, session.phone)
            self.memory.add_message(
                session,
                role="tool",
                content=json.dumps(result, ensure_ascii=False),
                tool_name="get_user_profile",
            )
            return (
                f"Profil bilgini buldum: {result['name']} olarak görünüyorsun. "
                f"Paketin: {result['plan']}."
            )

        if any(
            word in normalized
            for word in ["sunucu", "sistem", "ram", "disk", "cpu", "performans"]
        ):
            result = run_tool("check_local_system_status", {}, session.phone)
            self.memory.add_message(
                session,
                role="tool",
                content=json.dumps(result, ensure_ascii=False),
                tool_name="check_local_system_status",
            )
            return (
                "Yerel sistem durumu şöyle: "
                f"CPU %{result['cpu_percent']}, RAM %{result['ram_percent']} "
                f"({result['ram_available_gb']} GB uygun), disk %{result['disk_percent']} dolu."
            )

        return (
            "Mesajını aldım. Şu an lokal fallback modundayım; "
            "OpenAI veya OpenAI-compatible bir LLM ayarlarsan daha doğal ve araç destekli yanıtlar üretirim."
        )
