from __future__ import annotations

import asyncio
import sys

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient


async def _sessions_async(config: KamaConfig, limit: int) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        result = await client.send_command("session.list", {"limit": limit})
        sessions = result.get("sessions", [])
        if not sessions:
            print("no sessions")
            return 0
        for item in sessions:
            reason = f" ({item['interrupted_reason']})" if item.get("interrupted_reason") else ""
            print(
                f"{item['session_id']}  {item['status']}{reason}  "
                f"runs={item['run_count']}  {item['title']}"
            )
        return 0
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()


def cmd_sessions(config: KamaConfig, limit: int = 20) -> None:
    sys.exit(asyncio.run(_sessions_async(config, limit)))
