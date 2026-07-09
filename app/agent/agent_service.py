from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.agent.memory import ConversationMemory
from app.agent.tools import TOOL_DEFINITIONS, run_tool
from app.config import Settings
from app.schemas import AgentReply, AgentToolCall, SessionState

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

    async def generate_reply(self, session: SessionState) -> AgentReply:
        if self.client is None:
            return self._generate_fallback_reply(session)

        try:
            personal_memory_reply = self._generate_personal_memory_reply(session)
            if personal_memory_reply is not None:
                return personal_memory_reply

            forced_tool_reply = await self._generate_forced_tool_reply(session)
            if forced_tool_reply is not None:
                return forced_tool_reply

            tools_used: list[AgentToolCall] = []
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                *self.memory.to_openai_messages(session),
            ]
            if self._use_ollama_native_chat():
                return await self._generate_with_ollama_native(session, messages)

            first = await self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=self.settings.llm_temperature,
                max_tokens=self._max_generation_tokens(),
                extra_body=self._completion_extra_body(),
            )
            assistant_message = first.choices[0].message
            tool_calls = assistant_message.tool_calls or []

            if not tool_calls:
                return AgentReply(
                    text=self._sanitize_reply(
                        assistant_message.content or "Şu anda kısa bir yanıt üretemedim."
                    )
                )

            messages.append(assistant_message.model_dump(exclude_none=True))
            for tool_call in tool_calls:
                args = json.loads(tool_call.function.arguments or "{}")
                result = run_tool(tool_call.function.name, args, session.phone)
                tools_used.append(
                    AgentToolCall(
                        name=tool_call.function.name,
                        arguments=args,
                        result=result,
                    )
                )
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
                temperature=self.settings.llm_temperature,
                max_tokens=self._max_generation_tokens(),
                extra_body=self._completion_extra_body(),
            )
            return AgentReply(
                text=self._sanitize_reply(second.choices[0].message.content or "İşlemi tamamladım."),
                tools_used=tools_used,
            )
        except Exception:
            logger.exception("LLM generation failed, using local fallback")
            return self._generate_fallback_reply(session)

    async def rewrite_reply_for_constraints(self, text: str, max_words: int) -> str:
        original = text.strip()
        if not original or self.client is None:
            return original

        instruction = (
            "Aşağıdaki WhatsApp cevabını aynı anlamı koruyarak yeniden yaz.\n"
            f"En fazla {max_words} kelime kullan.\n"
            "Yarım cümle bırakma; kısa ama tamamlanmış doğal Türkçe cümle kur.\n"
            "Sığmayan fikri kesip atma, daha kısa ifade et.\n"
            "12 kelimeden kısa cevapta emoji kullanma.\n"
            "12-23 kelime arasında en fazla 1 emoji kullan.\n"
            "Sadece nihai cevabı yaz.\n\n"
            f"Cevap: {original}"
        )

        try:
            if self._use_ollama_native_chat():
                response = await self._ollama_chat(
                    messages=[
                        {"role": "system", "content": "/no_think\nKısa ve doğal Türkçe düzenleyicisin."},
                        {"role": "user", "content": instruction},
                    ]
                )
                content = response.get("message", {}).get("content")
                if content:
                    return self._sanitize_reply(content)

            response = await self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=[
                    {"role": "system", "content": "Kısa ve doğal Türkçe düzenleyicisin."},
                    {"role": "user", "content": instruction},
                ],
                temperature=self.settings.llm_temperature,
                max_tokens=self._max_generation_tokens(),
                extra_body=self._completion_extra_body(),
            )
            content = response.choices[0].message.content
            if content:
                return self._sanitize_reply(content)
        except Exception:
            logger.exception("Reply rewrite failed, using original text")

        return original

    def _generate_personal_memory_reply(self, session: SessionState) -> AgentReply | None:
        last_user_message = self._last_user_message(session)
        normalized = last_user_message.lower()
        known_name = session.facts.get("name")
        asks_for_name = any(
            phrase in normalized
            for phrase in ["adım ne", "ismim ne", "benim adım ne", "benim ismim ne"]
        )

        if asks_for_name:
            if known_name:
                return AgentReply(text=f"Adın {known_name}.")
            return AgentReply(text="Adını henüz bilmiyorum. İstersen adını söyleyebilirsin.")

        if known_name and any(
            phrase in normalized
            for phrase in ["benim adım", "adım ", "ismim ", "benim ismim"]
        ):
            return AgentReply(text=f"Memnun oldum {known_name}.")

        return None

    async def _generate_forced_tool_reply(self, session: SessionState) -> AgentReply | None:
        last_user_message = self._last_user_message(session)
        normalized = last_user_message.lower()

        if any(word in normalized for word in ["profil", "hesap", "plan", "ben kimim"]):
            return await self._summarize_tool_result(
                session=session,
                tool_name="get_user_profile",
                arguments={"phone": session.phone},
                result=run_tool("get_user_profile", {"phone": session.phone}, session.phone),
            )

        if any(
            word in normalized
            for word in ["sunucu", "sistem", "ram", "disk", "cpu", "performans"]
        ):
            return await self._summarize_tool_result(
                session=session,
                tool_name="check_local_system_status",
                arguments={},
                result=run_tool("check_local_system_status", {}, session.phone),
            )

        return None

    async def _summarize_tool_result(
        self,
        session: SessionState,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> AgentReply:
        tool_call = AgentToolCall(name=tool_name, arguments=arguments, result=result)
        result_text = json.dumps(result, ensure_ascii=False)
        self.memory.add_message(
            session,
            role="tool",
            content=result_text,
            tool_name=tool_name,
        )

        if self.client is not None:
            try:
                if self._use_ollama_native_chat():
                    response = await self._ollama_chat(
                        messages=[
                            {"role": "system", "content": self.system_prompt},
                            {
                                "role": "user",
                                "content": (
                                    "Kullanıcının son mesajına bu tool sonucuna göre kısa Türkçe cevap ver.\n"
                                    f"Kullanıcı mesajı: {self._last_user_message(session)}\n"
                                    f"Tool adı: {tool_name}\n"
                                    f"Tool sonucu: {result_text}"
                                ),
                            },
                        ]
                    )
                    content = response.get("message", {}).get("content")
                    if content:
                        return AgentReply(text=self._sanitize_reply(content), tools_used=[tool_call])

                response = await self.client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {
                            "role": "user",
                            "content": (
                                "Kullanıcının son mesajına bu tool sonucuna göre kısa Türkçe cevap ver.\n"
                                f"Kullanıcı mesajı: {self._last_user_message(session)}\n"
                                f"Tool adı: {tool_name}\n"
                                f"Tool sonucu: {result_text}"
                            ),
                        },
                    ],
                    temperature=self.settings.llm_temperature,
                    max_tokens=self._max_generation_tokens(),
                    extra_body=self._completion_extra_body(),
                )
                content = response.choices[0].message.content
                if content:
                    return AgentReply(text=self._sanitize_reply(content), tools_used=[tool_call])
            except Exception:
                logger.exception("Tool summary generation failed, using deterministic text")

        return AgentReply(
            text=self._sanitize_reply(self._deterministic_tool_reply(tool_name, result)),
            tools_used=[tool_call],
        )

    async def _generate_with_ollama_native(
        self,
        session: SessionState,
        messages: list[dict[str, Any]],
    ) -> AgentReply:
        first = await self._ollama_chat(messages=messages, tools=TOOL_DEFINITIONS)
        assistant_message = first.get("message", {})
        tool_calls = assistant_message.get("tool_calls") or []
        tools_used: list[AgentToolCall] = []

        if not tool_calls:
            content = assistant_message.get("content") or "Şu anda kısa bir yanıt üretemedim."
            return AgentReply(text=self._sanitize_reply(content))

        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        for tool_call in tool_calls:
            function_call = tool_call.get("function", {})
            tool_name = str(function_call.get("name") or "")
            args = self._parse_tool_arguments(function_call.get("arguments"))
            result = run_tool(tool_name, args, session.phone)
            tools_used.append(
                AgentToolCall(
                    name=tool_name,
                    arguments=args,
                    result=result,
                )
            )
            result_text = json.dumps(result, ensure_ascii=False)
            self.memory.add_message(
                session,
                role="tool",
                content=result_text,
                tool_name=tool_name,
            )
            messages.append({"role": "tool", "content": result_text})

        second = await self._ollama_chat(messages=messages)
        content = second.get("message", {}).get("content") or "İşlemi tamamladım."
        return AgentReply(text=self._sanitize_reply(content), tools_used=tools_used)

    async def _ollama_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "think": self.settings.llm_thinking_enabled,
            "options": {
                "temperature": self.settings.llm_temperature,
                "num_predict": self._max_generation_tokens(),
            },
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._ollama_base_url()}/api/chat", json=payload)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines()]
        cleaned = "\n".join(lines).strip()
        for emoji in ["🙂", "👍", "🌱", "✅"]:
            cleaned = cleaned.replace(f"{emoji}.", emoji)
            cleaned = cleaned.replace(f"{emoji}!", emoji)
        return cleaned

    def _completion_extra_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "options": {
                "num_predict": self._max_generation_tokens(),
                "temperature": self.settings.llm_temperature,
            }
        }
        if not self.settings.llm_thinking_enabled:
            body["think"] = False
        return body

    def _max_generation_tokens(self) -> int:
        return max(32, min(512, self.settings.agent_max_reply_words * 3))

    def _use_ollama_native_chat(self) -> bool:
        return bool(self.settings.llm_base_url and not self.settings.openai_api_key)

    def _ollama_base_url(self) -> str:
        base_url = (self.settings.llm_base_url or "").rstrip("/")
        if base_url.endswith("/v1"):
            return base_url[:-3]
        return base_url

    @staticmethod
    def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str) and arguments.strip():
            return json.loads(arguments)
        return {}

    @staticmethod
    def _deterministic_tool_reply(tool_name: str, result: dict[str, Any]) -> str:
        if tool_name == "get_user_profile":
            return (
                f"Profil bilgini buldum: {result.get('name', 'Misafir')} olarak görünüyorsun. "
                f"Paketin: {result.get('plan', 'standard')}."
            )
        if tool_name == "check_local_system_status":
            return (
                "Yerel sistem durumu şöyle: "
                f"CPU %{result.get('cpu_percent')}, RAM %{result.get('ram_percent')}, "
                f"disk %{result.get('disk_percent')} dolu."
            )
        return "İşlemi tamamladım."

    @staticmethod
    def _last_user_message(session: SessionState) -> str:
        return next(
            (item.content for item in reversed(session.memory) if item.role == "user"),
            "",
        )

    def _generate_fallback_reply(self, session: SessionState) -> AgentReply:
        personal_memory_reply = self._generate_personal_memory_reply(session)
        if personal_memory_reply is not None:
            return personal_memory_reply

        last_user_message = self._last_user_message(session)
        normalized = last_user_message.lower()

        if any(word in normalized for word in ["profil", "hesap", "plan", "ben kimim"]):
            result = run_tool("get_user_profile", {"phone": session.phone}, session.phone)
            tool_call = AgentToolCall(
                name="get_user_profile",
                arguments={"phone": session.phone},
                result=result,
            )
            self.memory.add_message(
                session,
                role="tool",
                content=json.dumps(result, ensure_ascii=False),
                tool_name="get_user_profile",
            )
            return AgentReply(
                text=(
                    f"Profil bilgini buldum: {result['name']} olarak görünüyorsun. "
                    f"Paketin: {result['plan']}."
                ),
                tools_used=[tool_call],
            )

        if any(
            word in normalized
            for word in ["sunucu", "sistem", "ram", "disk", "cpu", "performans"]
        ):
            result = run_tool("check_local_system_status", {}, session.phone)
            tool_call = AgentToolCall(
                name="check_local_system_status",
                arguments={},
                result=result,
            )
            self.memory.add_message(
                session,
                role="tool",
                content=json.dumps(result, ensure_ascii=False),
                tool_name="check_local_system_status",
            )
            return AgentReply(
                text=(
                    "Yerel sistem durumu şöyle: "
                    f"CPU %{result['cpu_percent']}, RAM %{result['ram_percent']} "
                    f"({result['ram_available_gb']} GB uygun), disk %{result['disk_percent']} dolu."
                ),
                tools_used=[tool_call],
            )

        return AgentReply(
            text=(
                "Mesajını aldım. Şu an lokal fallback modundayım; "
                "OpenAI veya OpenAI-compatible bir LLM ayarlarsan daha doğal ve araç destekli yanıtlar üretirim."
            ).strip()
        )
