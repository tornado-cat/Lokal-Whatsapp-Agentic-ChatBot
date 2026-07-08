from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.schemas import ParsedMessage

logger = logging.getLogger(__name__)


def _get_nested(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _clean_phone(remote_jid: str | None) -> str | None:
    if not remote_jid:
        return None
    if "@g.us" in remote_jid:
        logger.info("Ignoring group message remoteJid=%s", remote_jid)
        return None
    return remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "").strip()


def _extract_text(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    text = message.get("conversation")
    if text:
        return str(text).strip()
    extended_text = _get_nested(message, ["extendedTextMessage", "text"])
    if extended_text:
        return str(extended_text).strip()
    image_caption = _get_nested(message, ["imageMessage", "caption"])
    if image_caption:
        return str(image_caption).strip()
    video_caption = _get_nested(message, ["videoMessage", "caption"])
    if video_caption:
        return str(video_caption).strip()
    return ""


def parse_whatsapp_webhook(payload: dict[str, Any]) -> ParsedMessage | None:
    try:
        instance = str(payload.get("instance") or _get_nested(payload, ["data", "instance"]) or "")
        key = _get_nested(payload, ["data", "key"]) or {}
        remote_jid = key.get("remoteJid") if isinstance(key, dict) else None
        sender_phone = _clean_phone(str(remote_jid) if remote_jid else None)
        if not sender_phone:
            return None

        message = _get_nested(payload, ["data", "message"])
        text = _extract_text(message if isinstance(message, dict) else None)
        message_id = key.get("id") if isinstance(key, dict) else None
        from_me = bool(key.get("fromMe")) if isinstance(key, dict) else False

        return ParsedMessage(
            instance=instance or "default",
            sender_phone=sender_phone,
            text=text,
            message_id=str(message_id) if message_id else None,
            from_me=from_me,
        )
    except (ValidationError, ValueError, TypeError):
        logger.exception("Failed to parse Evolution webhook payload")
        return None
