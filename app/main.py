from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from datetime import datetime
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.agent.agent_service import AgentService
from app.agent.memory import ConversationMemory
from app.config import get_settings
from app.database import ConversationRepository
from app.logging_config import configure_logging
from app.schemas import (
    HealthResponse,
    ParsedMessage,
    SendMessageRequest,
    SendMessageResponse,
    WebhookAcceptedResponse,
)
from app.session_manager import SessionManager
from app.whatsapp.evolution_client import EvolutionClient
from app.whatsapp.webhook_parser import parse_whatsapp_webhook

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

session_manager = SessionManager()
memory = ConversationMemory(max_messages=settings.memory_max_messages)
agent_service = AgentService(settings=settings, memory=memory)
evolution_client = EvolutionClient(settings=settings)
conversation_repository = ConversationRepository(settings=settings)

app = FastAPI(title="Local WhatsApp Agent Backend", version="0.1.0")
DAILY_GREETING = (
    "Merhabalar ben AgroBoy bugün size nasıl yardımcı olabilirim?"
)

NAME_PATTERN = re.compile(
    r"(?:benim\s+ad[ıi]m|ad[ıi]m|benim\s+ismim|ismim)\s+([A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü\s'-]{1,40})",
    flags=re.IGNORECASE,
)
FACT_CONTEXT_PREFIX = "Bilinen kullanıcı bilgileri:"
REDUNDANT_OPENING_PATTERN = re.compile(
    r"^\s*(merhaba[!.]?\s*)?"
    r"((bug[uü]n\s+)?size\s+)?nas[ıi]l\s+yard[ıi]mc[ıi]\s+olabilirim[?.!]?\s*$",
    flags=re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"[\wÇĞİÖŞÜçğıöşü'-]+", flags=re.UNICODE)
SENTENCE_PATTERN = re.compile(r"[^.!?\n]+[.!?]?", flags=re.UNICODE)
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
pending_message_batches: dict[str, list[ParsedMessage]] = {}
pending_batch_tasks: dict[str, asyncio.Task[None]] = {}
pending_batches_guard = asyncio.Lock()


@app.on_event("startup")
async def startup() -> None:
    await conversation_repository.init()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="whatsapp-agent-backend")


@app.post("/messages/send", response_model=SendMessageResponse)
async def send_message(request: SendMessageRequest) -> SendMessageResponse:
    try:
        await evolution_client.send_text_message(
            instance=request.instance,
            phone=request.phone,
            text=request.text,
            raise_errors=True,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.json() if exc.response.content else exc.response.text,
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SendMessageResponse(
        status="sent",
        instance=request.instance,
        phone=request.phone,
    )


@app.get("/conversations")
async def list_conversations(
    session_key: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return await conversation_repository.list_turns(session_key=session_key, limit=limit)


@app.get("/facts")
async def list_facts(
    session_key: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return await conversation_repository.list_facts(session_key=session_key, limit=limit)


def _handle_evolution_status_error(exc: httpx.HTTPStatusError) -> HTTPException:
    return HTTPException(
        status_code=exc.response.status_code,
        detail=exc.response.json() if exc.response.content else exc.response.text,
    )


@app.get("/instances.json")
async def list_instances_json() -> list[dict[str, Any]]:
    try:
        return await evolution_client.fetch_instances()
    except httpx.HTTPStatusError as exc:
        raise _handle_evolution_status_error(exc) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _instance_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("instanceName") or "")


def _owner_phone(owner_jid: Any) -> str | None:
    if not owner_jid:
        return None
    return str(owner_jid).split("@", 1)[0].strip() or None


async def get_instance_summary(instance: str) -> dict[str, Any]:
    state_payload = await evolution_client.get_connection_state(instance)
    state = state_payload.get("instance", {}).get("state") or state_payload.get("state")
    instances = await evolution_client.fetch_instances()
    instance_data = next((item for item in instances if _instance_name(item) == instance), {})
    owner_jid = instance_data.get("ownerJid")
    owner_phone = _owner_phone(owner_jid)
    connection_status = instance_data.get("connectionStatus")
    connected = state == "open"
    qr_scanned = bool(owner_phone)

    return {
        "instance": instance,
        "state": state or "unknown",
        "connectionStatus": connection_status or "unknown",
        "connected": connected,
        "qrScanned": qr_scanned,
        "ownerJid": owner_jid,
        "ownerPhone": owner_phone,
        "profileName": instance_data.get("profileName"),
    }


@app.get("/instances", response_class=HTMLResponse)
async def list_instances_page() -> HTMLResponse:
    try:
        instances = await evolution_client.fetch_instances()
    except httpx.HTTPStatusError as exc:
        raise _handle_evolution_status_error(exc) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    rows = []
    for item in instances:
        name = escape(str(item.get("name") or item.get("instanceName") or "unknown"))
        status = escape(str(item.get("connectionStatus") or "unknown"))
        owner = escape(str(item.get("ownerJid") or "-").replace("@s.whatsapp.net", ""))
        rows.append(
            f"""
            <tr>
              <td>{name}</td>
              <td>{status}</td>
              <td>{owner}</td>
              <td>
                <a class="button" href="/instances/{name}/qr">QR Aç</a>
                <form method="post" action="/instances/{name}/logout">
                  <button type="submit">Çıkış Yap</button>
                </form>
              </td>
            </tr>
            """
        )

    table_body = "\n".join(rows) or '<tr><td colspan="4">Instance yok</td></tr>'
    return HTMLResponse(
        content=f"""
<!doctype html>
<html lang="tr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WhatsApp Instances</title>
    <style>
      body {{
        margin: 0;
        font-family: Arial, sans-serif;
        background: #f6f7f9;
        color: #101828;
      }}
      main {{
        max-width: 920px;
        margin: 48px auto;
        padding: 0 20px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        background: #fff;
        border: 1px solid #d0d5dd;
      }}
      th, td {{
        padding: 12px;
        border-bottom: 1px solid #eaecf0;
        text-align: left;
      }}
      th {{
        background: #f2f4f7;
      }}
      .button, button {{
        display: inline-block;
        margin-right: 8px;
        padding: 8px 10px;
        border: 1px solid #98a2b3;
        background: #fff;
        color: #101828;
        text-decoration: none;
        cursor: pointer;
        font-size: 14px;
      }}
      form {{
        display: inline;
      }}
      p {{
        color: #475467;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>WhatsApp Instances</h1>
      <p>Bağlı instance için QR görünmez. Yanlış numara bağlıysa önce Çıkış Yap, sonra QR Aç.</p>
      <table>
        <thead>
          <tr>
            <th>Instance</th>
            <th>Durum</th>
            <th>Bağlı Numara</th>
            <th>Aksiyon</th>
          </tr>
        </thead>
        <tbody>{table_body}</tbody>
      </table>
    </main>
  </body>
</html>
""".strip()
    )


@app.post("/instances/{instance}/logout")
async def logout_instance(instance: str) -> RedirectResponse:
    try:
        await evolution_client.logout_instance(instance)
    except httpx.HTTPStatusError as exc:
        raise _handle_evolution_status_error(exc) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(url=f"/instances/{instance}/qr", status_code=303)


@app.get("/instances/{instance}/qr.png")
async def instance_qr_png(instance: str) -> Response:
    try:
        summary = await get_instance_summary(instance)
        if summary["connected"] or summary["qrScanned"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "QR already scanned or instance connected",
                    "instance": instance,
                    "ownerPhone": summary["ownerPhone"],
                    "state": summary["state"],
                },
            )
        png = await evolution_client.get_qr_png(instance)
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.json() if exc.response.content else exc.response.text,
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch QR image for instance=%s", instance)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/instances/{instance}/status")
async def instance_status(instance: str) -> dict[str, Any]:
    try:
        return await evolution_client.get_connection_state(instance)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.json() if exc.response.content else exc.response.text,
        ) from exc
    except Exception as exc:
        logger.exception("Failed to fetch status for instance=%s", instance)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/instances/{instance}/summary")
async def instance_summary(instance: str) -> dict[str, Any]:
    try:
        return await get_instance_summary(instance)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.json() if exc.response.content else exc.response.text,
        ) from exc
    except Exception as exc:
        logger.exception("Failed to fetch summary for instance=%s", instance)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/instances/{instance}/qr", response_class=HTMLResponse)
async def instance_qr_page(instance: str) -> HTMLResponse:
    safe_instance = escape(instance)
    return HTMLResponse(
        content=f"""
<!doctype html>
<html lang="tr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_instance} WhatsApp QR</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: Arial, sans-serif;
        background: #f6f7f9;
        color: #101828;
      }}
      main {{
        width: min(92vw, 440px);
        text-align: center;
      }}
      img {{
        width: min(82vw, 348px);
        height: auto;
        background: white;
        padding: 16px;
        border: 1px solid #d0d5dd;
      }}
      h1 {{
        margin: 0 0 18px;
        font-size: 22px;
      }}
      p {{
        margin: 16px 0 0;
        color: #475467;
        font-size: 14px;
      }}
      #status {{
        display: inline-block;
        margin: 0 0 14px;
        padding: 6px 10px;
        border: 1px solid #d0d5dd;
        background: #fff;
        font-size: 13px;
      }}
      #owner {{
        min-height: 22px;
        margin: 0 0 14px;
        color: #344054;
        font-size: 14px;
        font-weight: 700;
      }}
      #qrWrap {{
        min-height: 386px;
      }}
      button {{
        margin-top: 16px;
        padding: 10px 14px;
        border: 1px solid #98a2b3;
        background: #fff;
        cursor: pointer;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{safe_instance} WhatsApp QR</h1>
      <div id="status">Durum kontrol ediliyor...</div>
      <div id="owner"></div>
      <div id="qrWrap">
        <img id="qr" src="/instances/{safe_instance}/qr.png?t=0" alt="{safe_instance} WhatsApp QR" />
      </div>
      <p>WhatsApp > Bağlı Cihazlar > Cihaz Bağla</p>
      <p>QR okunduktan sonra bu sayfayı kapat. Yeni QR sadece gerekirse manuel alınır.</p>
      <button id="refreshButton" type="button" onclick="refreshQr()">Yeni QR Al</button>
    </main>
    <script>
      const instance = "{safe_instance}";
      const statusEl = document.getElementById("status");
      const ownerEl = document.getElementById("owner");
      const qrEl = document.getElementById("qr");
      const qrWrapEl = document.getElementById("qrWrap");
      const refreshButton = document.getElementById("refreshButton");
      let qrLocked = false;
      let statusTimer = null;

      function refreshQr() {{
        if (qrLocked) {{
          return;
        }}
        qrEl.src = `/instances/${{instance}}/qr.png?t=${{Date.now()}}`;
      }}

      function stopQr(summary) {{
        qrLocked = true;
        qrEl.removeAttribute("src");
        qrEl.style.display = "none";
        qrWrapEl.style.display = "none";
        refreshButton.disabled = true;
        refreshButton.textContent = "QR durduruldu";
        const owner = summary.profileName || summary.ownerPhone || summary.ownerJid || "bilinmiyor";
        ownerEl.textContent = `Bağlanan hesap: ${{owner}}`;
      }}

      async function pollStatus() {{
        try {{
          const response = await fetch(`/instances/${{instance}}/summary`, {{ cache: "no-store" }});
          const data = await response.json();
          const state = data.state || "unknown";
          statusEl.textContent = `Durum: ${{state}}`;
          if (data.ownerPhone && data.state === "close") {{
            ownerEl.textContent = `Son bağlı hesap: ${{data.profileName || data.ownerPhone || data.ownerJid}}`;
          }}
          if (data.connected || data.qrScanned) {{
            stopQr(data);
          }}
          if (data.connected) {{
            statusEl.textContent = "Durum: bağlı";
            if (statusTimer) {{
              clearInterval(statusTimer);
            }}
          }} else if (data.qrScanned) {{
            statusEl.textContent = `Durum: bağlanıyor (${{state}})`;
          }} else if (state === "close") {{
            statusEl.textContent = "Durum: bağlantı kapalı";
          }}
        }} catch (error) {{
          statusEl.textContent = "Durum alınamadı";
        }}
      }}

      pollStatus();
      statusTimer = setInterval(pollStatus, 10000);
    </script>
  </body>
</html>
""".strip()
    )


@app.post("/webhook/whatsapp", response_model=WebhookAcceptedResponse)
async def whatsapp_webhook(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
) -> WebhookAcceptedResponse:
    logger.info(
        "Webhook received keys=%s instance=%s event=%s",
        list(payload.keys()),
        payload.get("instance"),
        payload.get("event"),
    )
    logger.debug("Webhook payload preview=%s", str(payload)[:1000])

    parsed = parse_whatsapp_webhook(payload)
    if parsed is None:
        return WebhookAcceptedResponse(status="ignored", detail="unsupported payload")
    if parsed.from_me:
        logger.info("Ignoring fromMe message id=%s instance=%s", parsed.message_id, parsed.instance)
        return WebhookAcceptedResponse(status="ignored", detail="fromMe message")
    if not parsed.text.strip():
        logger.info("Ignoring empty message id=%s instance=%s", parsed.message_id, parsed.instance)
        return WebhookAcceptedResponse(status="ignored", detail="empty message")

    allowed_senders = settings.allowed_sender_set
    normalized_sender = parsed.sender_phone.replace("+", "")
    if allowed_senders and normalized_sender not in allowed_senders:
        logger.info(
            "Ignoring sender outside allowlist instance=%s sender=%s",
            parsed.instance,
            parsed.sender_phone,
        )
        return WebhookAcceptedResponse(status="ignored", detail="sender not allowed")

    background_tasks.add_task(enqueue_incoming_message, parsed)
    return WebhookAcceptedResponse(status="accepted", detail="message queued")


async def enqueue_incoming_message(parsed_message: ParsedMessage) -> None:
    session_key = SessionManager.build_session_key(
        tenant_id=parsed_message.instance,
        user_id=parsed_message.sender_phone,
    )
    async with pending_batches_guard:
        pending_message_batches.setdefault(session_key, []).append(parsed_message)
        existing_task = pending_batch_tasks.get(session_key)
        if existing_task and not existing_task.done():
            existing_task.cancel()
        pending_batch_tasks[session_key] = asyncio.create_task(
            process_pending_batch_after_delay(session_key)
        )


async def process_pending_batch_after_delay(session_key: str) -> None:
    try:
        await asyncio.sleep(settings.message_batch_delay_seconds)
    except asyncio.CancelledError:
        return

    async with pending_batches_guard:
        batch = pending_message_batches.pop(session_key, [])
        pending_batch_tasks.pop(session_key, None)

    if not batch:
        return

    await process_incoming_message(merge_message_batch(batch))


def merge_message_batch(batch: list[ParsedMessage]) -> ParsedMessage:
    last_message = batch[-1]
    merged_text = "\n".join(message.text.strip() for message in batch if message.text.strip())
    merged_message_ids = ",".join(message.message_id or "" for message in batch if message.message_id)
    return last_message.model_copy(
        update={
            "text": merged_text,
            "message_id": merged_message_ids or last_message.message_id,
        }
    )


async def process_incoming_message(parsed_message: ParsedMessage) -> None:
    session_key, session = await session_manager.get_or_create(
        tenant_id=parsed_message.instance,
        phone=parsed_message.sender_phone,
    )
    lock = await session_manager.get_lock(session_key)

    async with lock:
        try:
            await hydrate_session_memory(session_key, session)
            is_first_message_today = not await conversation_repository.has_turn_today(session_key)
            session.updated_at = datetime.utcnow()

            extracted_name = extract_user_name(parsed_message.text)
            if extracted_name:
                await conversation_repository.upsert_fact(
                    session_key=session_key,
                    tenant_id=parsed_message.instance,
                    phone=parsed_message.sender_phone,
                    fact_key="name",
                    fact_value=extracted_name,
                )
                session.facts["name"] = extracted_name

            sync_fact_context(session)
            memory.add_message(session, role="user", content=parsed_message.text)
            logger.info(
                "Processing session=%s message_id=%s",
                session_key,
                parsed_message.message_id,
            )
            reply = await agent_service.generate_reply(session)
            if is_first_message_today:
                reply.text = remove_redundant_opening(reply.text)
                reply.text = f"{DAILY_GREETING}\n\n{reply.text}".strip()
            if reply_needs_rewrite(reply.text, settings.agent_max_reply_words):
                reply.text = await agent_service.rewrite_reply_for_constraints(
                    reply.text,
                    settings.agent_max_reply_words,
                )
            reply.text = finalize_reply(reply.text, settings.agent_max_reply_words)
            memory.add_message(session, role="assistant", content=reply.text)
            await conversation_repository.save_turn(
                session_key=session_key,
                tenant_id=parsed_message.instance,
                phone=parsed_message.sender_phone,
                message_id=parsed_message.message_id,
                user_message=parsed_message.text,
                assistant_message=reply.text,
                tools_used=[tool.model_dump() for tool in reply.tools_used],
            )
            await evolution_client.send_text_message(
                instance=parsed_message.instance,
                phone=parsed_message.sender_phone,
                text=reply.text,
            )
        except Exception:
            logger.exception("Failed to process incoming message session=%s", session_key)


async def hydrate_session_memory(session_key: str, session: Any) -> None:
    if session.memory:
        session.facts = await conversation_repository.get_facts(session_key)
        sync_fact_context(session)
        return

    session.facts = await conversation_repository.get_facts(session_key)
    sync_fact_context(session)

    recent_turns = await conversation_repository.get_recent_turns(
        session_key=session_key,
        limit=max(1, settings.memory_max_messages // 2),
    )
    for turn in recent_turns:
        memory.add_message(session, role="user", content=str(turn["user_message"]))
        memory.add_message(session, role="assistant", content=str(turn["assistant_message"]))


def sync_fact_context(session: Any) -> None:
    session.memory = [
        item
        for item in session.memory
        if not (item.role == "system" and item.content.startswith(FACT_CONTEXT_PREFIX))
    ]
    if not session.facts:
        return

    facts_text = ", ".join(f"{key}: {value}" for key, value in sorted(session.facts.items()))
    session.memory.insert(
        0,
        memory_message(role="system", content=f"{FACT_CONTEXT_PREFIX} {facts_text}"),
    )


def memory_message(role: str, content: str) -> Any:
    from app.schemas import MemoryMessage

    return MemoryMessage(role=role, content=content)  # type: ignore[arg-type]


def extract_user_name(text: str) -> str | None:
    candidates = [line.strip() for line in text.splitlines() if line.strip()]
    if not candidates:
        candidates = [text.strip()]

    match: re.Match[str] | None = None
    for candidate in candidates:
        normalized = candidate.lower()
        if any(
            phrase in normalized
            for phrase in ["adım ne", "ismim ne", "benim adım ne", "benim ismim ne"]
        ):
            continue
        match = NAME_PATTERN.search(candidate)
        if match:
            break

    if match is None:
        return None
    raw_name = match.group(1).strip(" .,!?:;")
    first_part = raw_name.split()[0] if raw_name else ""
    if first_part.lower() in {"ne", "kim", "nedir", "neydi"}:
        return None
    if not first_part:
        return None
    return first_part[:1].upper() + first_part[1:]


def finalize_reply(text: str, max_words: int) -> str:
    return enforce_emoji_budget(limit_words(text, max_words))


def reply_needs_rewrite(text: str, max_words: int) -> bool:
    stripped = text.strip()
    if max_words > 0 and count_words(stripped) > max_words:
        return True
    return has_incomplete_trailing_sentence(stripped)


def limit_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return text.strip()
    stripped = drop_incomplete_trailing_sentence(text.strip())
    if count_words(stripped) <= max_words:
        return stripped

    complete_prefixes: list[str] = []
    cursor = 0
    for match in SENTENCE_PATTERN.finditer(stripped):
        sentence = match.group(0).strip()
        if not sentence:
            continue
        cursor = match.end()
        prefix = stripped[:cursor].strip()
        if count_words(prefix) <= max_words and sentence[-1:] in ".!?":
            complete_prefixes.append(prefix)

    if complete_prefixes:
        return complete_prefixes[-1].strip()

    first_sentence = next(
        (match.group(0).strip() for match in SENTENCE_PATTERN.finditer(stripped) if match.group(0).strip()),
        "",
    )
    if first_sentence and count_words(first_sentence) <= max_words:
        return ensure_sentence_end(first_sentence)

    words = WORD_PATTERN.findall(stripped)
    if not words:
        return stripped[: max_words * 6].strip()
    return ensure_sentence_end(" ".join(words[:max_words]).strip())


def drop_incomplete_trailing_sentence(text: str) -> str:
    stripped = text.strip()
    last_end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
    if last_end == -1 or last_end == len(stripped) - 1:
        return stripped
    trailing = stripped[last_end + 1 :].strip()
    if not trailing:
        return stripped[: last_end + 1].strip()
    return stripped[: last_end + 1].strip()


def has_incomplete_trailing_sentence(text: str) -> bool:
    stripped = text.strip()
    last_end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
    if last_end == -1:
        return False
    trailing = stripped[last_end + 1 :].strip()
    return bool(trailing)


def count_words(text: str) -> int:
    return len(WORD_PATTERN.findall(text))


def ensure_sentence_end(text: str) -> str:
    stripped = text.strip(" ,;:")
    if not stripped:
        return stripped
    if stripped[-1] in ".!?🙂👍🌱✅":
        return stripped
    return f"{stripped}."


def enforce_emoji_budget(text: str) -> str:
    word_count = count_words(text)
    allowed_emoji_count = word_count // 12
    kept = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal kept
        if kept < allowed_emoji_count:
            kept += 1
            return match.group(0)
        return ""

    cleaned = EMOJI_PATTERN.sub(replace, text)
    return re.sub(r" {2,}", " ", cleaned).strip()


def remove_redundant_opening(text: str) -> str:
    stripped = text.strip()
    if REDUNDANT_OPENING_PATTERN.match(stripped):
        return ""
    return stripped
