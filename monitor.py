"""
WebSocket monitor: subscribes to pump.fun program logs and emits CreateEvents
as fast as possible using 'processed' commitment.
"""
import asyncio
import base64
import json
import logging
from typing import AsyncIterator

import websockets

from config import RPC_WS_URL
from pumpfun import PUMP_PROGRAM_ID, CREATE_EVENT_DISCRIMINATOR, CreateEvent

log = logging.getLogger(__name__)

SUBSCRIBE_MSG = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "logsSubscribe",
    "params": [
        {"mentions": [str(PUMP_PROGRAM_ID)]},
        {"commitment": "processed"},
    ],
})


async def stream_create_events() -> AsyncIterator[CreateEvent]:
    """
    Yield CreateEvent objects as new pump.fun tokens are launched.
    Reconnects automatically on disconnect.
    """
    backoff = 1
    while True:
        try:
            async with websockets.connect(
                RPC_WS_URL,
                ping_interval=20,
                ping_timeout=30,
                max_size=10 * 1024 * 1024,
            ) as ws:
                await ws.send(SUBSCRIBE_MSG)
                log.info("WebSocket connected; subscribed to pump.fun logs")
                backoff = 1

                async for raw in ws:
                    msg = json.loads(raw)
                    event = _parse_message(msg)
                    if event:
                        yield event

        except (websockets.ConnectionClosed, ConnectionResetError, OSError) as exc:
            log.warning("WS disconnected (%s); reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as exc:
            log.error("Unexpected WS error: %s", exc, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _parse_message(msg: dict) -> CreateEvent | None:
    try:
        logs: list[str] = msg["params"]["result"]["value"]["logs"]
    except (KeyError, TypeError):
        return None

    for line in logs:
        if not line.startswith("Program data: "):
            continue
        raw = base64.b64decode(line[len("Program data: "):])
        if len(raw) < 8:
            continue
        if raw[:8] != CREATE_EVENT_DISCRIMINATOR:
            continue
        return CreateEvent.decode(raw)

    return None
