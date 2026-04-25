"""
WebSocket monitor: subscribes to pump.fun program logs and emits CreateEvents
at processed commitment (fastest possible finality tier).
"""
import asyncio
import base64
import logging
from typing import AsyncIterator

import orjson
import websockets

from config import RPC_WS_URL
from pumpfun import PUMP_PROGRAM_ID, CREATE_EVENT_DISCRIMINATOR, CreateEvent

log = logging.getLogger(__name__)

# Pre-compute constants used in the hot-path message parser
_DATA_PREFIX     = "Program data: "
_DATA_PREFIX_LEN = len(_DATA_PREFIX)
_DISCRIMINATOR   = CREATE_EVENT_DISCRIMINATOR  # bytes, pre-imported

_SUBSCRIBE_BYTES = orjson.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "logsSubscribe",
    "params": [
        {"mentions": [str(PUMP_PROGRAM_ID)]},
        {"commitment": "processed"},
    ],
})


async def stream_create_events() -> AsyncIterator[CreateEvent]:
    """Yield CreateEvents for every new pump.fun token launch. Auto-reconnects."""
    backoff = 1
    while True:
        try:
            async with websockets.connect(
                RPC_WS_URL,
                ping_interval=20,
                ping_timeout=30,
                max_size=10 * 1024 * 1024,
                open_timeout=15,
            ) as ws:
                await ws.send(_SUBSCRIBE_BYTES)
                log.info("WebSocket connected — subscribed to pump.fun logs")
                backoff = 1

                async for raw in ws:
                    event = _parse(raw)
                    if event:
                        yield event

        except (websockets.ConnectionClosed, websockets.InvalidStatus,
                ConnectionResetError, OSError) as exc:
            log.warning("WS disconnected (%s); reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as exc:
            log.error("WS error: %s", exc, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _parse(raw: str | bytes) -> CreateEvent | None:
    try:
        msg  = orjson.loads(raw)
        logs: list[str] = msg["params"]["result"]["value"]["logs"]
    except (KeyError, TypeError, orjson.JSONDecodeError):
        return None

    for line in logs:
        # Fast prefix check before any allocation
        if line[:_DATA_PREFIX_LEN] != _DATA_PREFIX:
            continue
        data = base64.b64decode(line[_DATA_PREFIX_LEN:])
        if len(data) >= 8 and data[:8] == _DISCRIMINATOR:
            return CreateEvent.decode(data)

    return None
