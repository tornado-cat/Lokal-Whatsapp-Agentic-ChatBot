from __future__ import annotations

import logging
from html import escape
from datetime import datetime
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.agent.agent_service import AgentService
from app.agent.memory import ConversationMemory
from app.config import get_settings
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

app = FastAPI(title="Local WhatsApp Agent Backend", version="0.1.0")


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
      <img id="qr" src="/instances/{safe_instance}/qr.png?t=0" alt="{safe_instance} WhatsApp QR" />
      <p>WhatsApp > Bağlı Cihazlar > Cihaz Bağla</p>
      <button type="button" onclick="refreshQr()">Yeni QR Al</button>
    </main>
    <script>
      const instance = "{safe_instance}";
      const statusEl = document.getElementById("status");
      const qrEl = document.getElementById("qr");

      function refreshQr() {{
        qrEl.src = `/instances/${{instance}}/qr.png?t=${{Date.now()}}`;
      }}

      async function pollStatus() {{
        try {{
          const response = await fetch(`/instances/${{instance}}/status`, {{ cache: "no-store" }});
          const data = await response.json();
          const state = data.instance?.state || data.state || "unknown";
          statusEl.textContent = `Durum: ${{state}}`;
          if (state === "open") {{
            statusEl.textContent = "Durum: bağlı";
            qrEl.style.display = "none";
          }}
        }} catch (error) {{
          statusEl.textContent = "Durum alınamadı";
        }}
      }}

      pollStatus();
      setInterval(pollStatus, 2000);
      setInterval(refreshQr, 45000);
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

    background_tasks.add_task(process_incoming_message, parsed)
    return WebhookAcceptedResponse(status="accepted", detail="message queued")


async def process_incoming_message(parsed_message: ParsedMessage) -> None:
    session_key, session = await session_manager.get_or_create(
        tenant_id=parsed_message.instance,
        phone=parsed_message.sender_phone,
    )
    lock = await session_manager.get_lock(session_key)

    async with lock:
        try:
            session.updated_at = datetime.utcnow()
            memory.add_message(session, role="user", content=parsed_message.text)
            logger.info(
                "Processing session=%s message_id=%s",
                session_key,
                parsed_message.message_id,
            )
            reply = await agent_service.generate_reply(session)
            memory.add_message(session, role="assistant", content=reply)
            await evolution_client.send_text_message(
                instance=parsed_message.instance,
                phone=parsed_message.sender_phone,
                text=reply,
            )
        except Exception:
            logger.exception("Failed to process incoming message session=%s", session_key)
