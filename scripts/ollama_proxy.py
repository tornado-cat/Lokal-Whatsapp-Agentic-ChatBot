from __future__ import annotations

import asyncio
import logging
import os


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [ollama-proxy] %(message)s",
)
logger = logging.getLogger(__name__)


LISTEN_HOST = os.getenv("OLLAMA_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("OLLAMA_PROXY_LISTEN_PORT", "11435"))
TARGET_HOST = os.getenv("OLLAMA_PROXY_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.getenv("OLLAMA_PROXY_TARGET_PORT", "11434"))


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        target_reader, target_writer = await asyncio.open_connection(
            TARGET_HOST,
            TARGET_PORT,
        )
    except OSError as exc:
        logger.error("Could not connect to Ollama target: %s", exc)
        client_writer.close()
        await client_writer.wait_closed()
        return

    await asyncio.gather(
        pipe(client_reader, target_writer),
        pipe(target_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    logger.info(
        "Listening on %s:%s and forwarding to %s:%s",
        LISTEN_HOST,
        LISTEN_PORT,
        TARGET_HOST,
        TARGET_PORT,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
