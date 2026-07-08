from app.schemas import MemoryMessage, SessionState


class ConversationMemory:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages

    def add_message(
        self,
        session: SessionState,
        role: str,
        content: str,
        tool_name: str | None = None,
    ) -> None:
        session.memory.append(
            MemoryMessage(role=role, content=content, tool_name=tool_name)  # type: ignore[arg-type]
        )
        if len(session.memory) > self.max_messages:
            session.memory = session.memory[-self.max_messages :]

    def to_openai_messages(self, session: SessionState) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in session.memory[-self.max_messages :]:
            if item.role == "tool":
                messages.append(
                    {
                        "role": "system",
                        "content": f"Tool sonucu ({item.tool_name or 'unknown'}): {item.content}",
                    }
                )
            else:
                messages.append({"role": item.role, "content": item.content})
        return messages
