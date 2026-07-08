from __future__ import annotations

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
        self.timeout = httpx.Timeout(15.0, connect=5.0)

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

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                logger.debug("Evolution sendText response: %s", response.text[:500])
                response.raise_for_status()
                logger.info("Sent WhatsApp reply via instance=%s phone=%s", instance, phone)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Evolution API HTTP error status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            if raise_errors:
                raise
        except httpx.HTTPError:
            logger.exception("Evolution API request failed")
            if raise_errors:
                raise
