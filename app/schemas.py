from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ParsedMessage(BaseModel):
    instance: str
    sender_phone: str
    text: str
    message_id: str | None = None
    from_me: bool = False


class MemoryMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    tool_name: str | None = None


class SessionState(BaseModel):
    tenant_id: str
    phone: str
    memory: list[MemoryMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HealthResponse(BaseModel):
    status: str
    service: str


class WebhookAcceptedResponse(BaseModel):
    status: str
    detail: str


class SendMessageRequest(BaseModel):
    instance: str
    phone: str
    text: str


class SendMessageResponse(BaseModel):
    status: str
    instance: str
    phone: str


ToolResult = dict[str, Any]
