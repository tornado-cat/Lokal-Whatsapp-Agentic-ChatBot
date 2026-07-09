from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class EvolutionClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.evolution_api_base_url.rstrip("/")
        self.api_key = settings.evolution_api_key
        self.timeout = httpx.Timeout(30.0, connect=5.0)
        self._send_locks: dict[str, asyncio.Lock] = {}

    def _send_lock_for(self, instance: str) -> asyncio.Lock:
        lock = self._send_locks.get(instance)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[instance] = lock
        return lock

    async def connect_instance(self, instance: str) -> dict[str, Any]:
        url = f"{self.base_url}/instance/connect/{instance}"
        headers = {"apikey": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers)
            logger.debug("Evolution connect response: %s", response.text[:500])
            response.raise_for_status()
            return response.json()

    async def get_connection_state(self, instance: str) -> dict[str, Any]:
        url = f"{self.base_url}/instance/connectionState/{instance}"
        headers = {"apikey": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers)
            logger.debug("Evolution connectionState response: %s", response.text[:500])
            response.raise_for_status()
            return response.json()

    async def fetch_instances(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/instance/fetchInstances"
        headers = {"apikey": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers)
            logger.debug("Evolution fetchInstances response: %s", response.text[:500])
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            return []

    async def logout_instance(self, instance: str) -> dict[str, Any]:
        url = f"{self.base_url}/instance/logout/{instance}"
        headers = {"apikey": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.delete(url, headers=headers)
            logger.debug("Evolution logout response: %s", response.text[:500])
            response.raise_for_status()
            return response.json() if response.content else {"status": "ok"}

    async def get_qr_png(self, instance: str) -> bytes:
        payload = await self.connect_instance(instance)
        base64_qr = payload.get("base64")
        if not isinstance(base64_qr, str) or not base64_qr:
            raise ValueError("Evolution response did not include a QR base64 image")

        encoded = base64_qr.removeprefix("data:image/png;base64,")
        return base64.b64decode(encoded)

    async def wait_until_open(self, instance: str, max_wait_seconds: int = 20) -> bool:
        deadline = asyncio.get_running_loop().time() + max_wait_seconds
        while True:
            try:
                state_payload = await self.get_connection_state(instance)
                state = state_payload.get("instance", {}).get("state") or state_payload.get("state")
                if state == "open":
                    return True
                logger.info("Waiting for Evolution instance=%s state=%s", instance, state)
            except httpx.HTTPError:
                logger.exception("Failed to read Evolution state before send instance=%s", instance)

            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(2)

    async def send_text_message(
        self,
        instance: str,
        phone: str,
        text: str,
        raise_errors: bool = False,
    ) -> None:
        url = f"{self.base_url}/message/sendText/{instance}"
        payload = {"number": phone.replace("+", "").strip(), "text": text}
        headers = {"apikey": self.api_key}

        async with self._send_lock_for(instance):
            last_http_error: httpx.HTTPStatusError | None = None
            for attempt in range(1, 6):
                is_open = await self.wait_until_open(instance)
                if not is_open:
                    logger.error("Evolution instance did not become open before send instance=%s", instance)

                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(url, json=payload, headers=headers)
                        logger.debug("Evolution sendText response: %s", response.text[:500])
                        response.raise_for_status()
                        logger.info("Sent WhatsApp reply via instance=%s phone=%s", instance, phone)
                        return
                except httpx.HTTPStatusError as exc:
                    last_http_error = exc
                    body = exc.response.text[:500]
                    retryable = exc.response.status_code >= 500 or "Connection Closed" in body
                    logger.error(
                        "Evolution API HTTP error attempt=%s status=%s body=%s",
                        attempt,
                        exc.response.status_code,
                        body,
                    )
                    if not retryable or attempt == 5:
                        if raise_errors:
                            raise
                        return
                    await asyncio.sleep(min(3 * attempt, 10))
                except httpx.HTTPError:
                    logger.exception("Evolution API request failed attempt=%s", attempt)
                    if attempt == 5:
                        if raise_errors:
                            raise
                        return
                    await asyncio.sleep(min(3 * attempt, 10))

            if raise_errors and last_http_error:
                raise last_http_error
