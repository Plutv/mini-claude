from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpCallTimeoutError(McpServerUnavailableError):
    pass


class McpToolError(Exception):
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    read_only_hint: bool = False


class McpClient:
    """Concurrent JSON-RPC client with one reader and id-routed pending futures."""

    _STREAM_LIMIT = 64 * 1024 * 1024

    def __init__(self, *, call_timeout_s: float = 30.0) -> None:
        self._id = 0
        self._call_timeout_s = call_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done() and not self._closing

    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        self._reset_connection_state()
        merged_env = {**os.environ, **(env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=self._STREAM_LIMIT,
        )
        self._reader = self._proc.stdout
        self._transport = "stdio"
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._initialize()

    async def connect_tcp(self, host: str, port: int) -> None:
        self._reset_connection_state()
        self._reader, self._tcp_writer = await asyncio.open_connection(
            host, port, limit=self._STREAM_LIMIT
        )
        self._transport = "tcp"
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._initialize()

    def _reset_connection_state(self) -> None:
        self._closing = False
        self._closed = asyncio.Event()

    async def _initialize(self) -> None:
        await self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kama-claude", "version": "0.1"},
            },
        )
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[McpToolDef]:
        response = await self._call("tools/list", {})
        tools = []
        for tool in response.get("tools", []):
            annotations = tool.get("annotations") or {}
            tools.append(
                McpToolDef(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                    read_only_hint=bool(annotations.get("readOnlyHint", False)),
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._call("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError"):
            text = "\n".join(
                str(item.get("text", ""))
                for item in response.get("content", [])
                if item.get("type") == "text"
            )
            raise McpToolError(text or f"MCP tool {name!r} failed")
        return "\n".join(
            str(item["text"])
            for item in response.get("content", [])
            if item.get("type") == "text"
        )

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while line := await self._proc.stderr.readline():
                stderr_line = line.decode(errors="replace").rstrip()
                if stderr_line:
                    log.debug("mcp stderr: %s", stderr_line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("mcp stderr drain stopped", exc_info=True)

    async def _reader_loop(self) -> None:
        failure: Exception = McpServerUnavailableError("MCP server closed connection")
        try:
            while True:
                line = await self._read_line()
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("mcp: ignoring non-JSON line: %r", line[:200])
                    continue
                message_id = message.get("id")
                if message_id is None:
                    log.debug("mcp: received server notification: %s", message.get("method"))
                    continue
                future = self._pending.pop(str(message_id), None)
                if future is None or future.done():
                    log.debug("mcp: late or unknown response id=%s", message_id)
                    continue
                if "error" in message:
                    error = message["error"]
                    future.set_exception(
                        McpToolError(
                            f"{error.get('message', str(error))} (code={error.get('code')})"
                        )
                    )
                else:
                    future.set_result(message.get("result") or {})
        except asyncio.CancelledError:
            failure = McpServerUnavailableError("MCP client closed")
            raise
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, McpServerUnavailableError)
                else McpServerUnavailableError(str(exc))
            )
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            self._pending.clear()
            self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._closing = True
        reader_task = self._reader_task
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        tasks = [task for task in (reader_task, self._stderr_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        if self._transport == "stdio" and self._proc is not None:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        elif self._transport == "tcp" and self._tcp_writer is not None:
            self._tcp_writer.close()
            try:
                await self._tcp_writer.wait_closed()
            except OSError:
                pass
        self._closed.set()

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            raise McpServerUnavailableError("MCP client is not connected")
        self._id += 1
        request_id = str(self._id)
        request = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(request)
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._call_timeout_s)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            await self._notify("notifications/cancelled", {"requestId": self._id})
            raise McpCallTimeoutError(
                f"MCP call timed out method={method} timeout={self._call_timeout_s}s"
            ) from exc
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write_message(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message) + "\n").encode()
        async with self._write_lock:
            writer: Any
            if self._transport == "stdio":
                writer = self._proc.stdin if self._proc else None
            else:
                writer = self._tcp_writer
            if writer is None:
                raise McpServerUnavailableError("MCP writer unavailable")
            try:
                writer.write(data)
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise McpServerUnavailableError(str(exc)) from exc

    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("MCP reader unavailable")
        try:
            data = await self._reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise McpServerUnavailableError(
                f"MCP response too large (>{self._STREAM_LIMIT // 1024 // 1024}MB)"
            ) from exc
        except (ConnectionResetError, OSError) as exc:
            raise McpServerUnavailableError(str(exc)) from exc
        if not data:
            raise McpServerUnavailableError("MCP server closed connection")
        return data.decode(errors="replace").strip()
