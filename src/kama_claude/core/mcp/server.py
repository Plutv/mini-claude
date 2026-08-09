from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from kama_claude.core.config import McpServerConfig
from kama_claude.core.mcp.client import McpClient, McpServerUnavailableError, McpToolDef
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class McpServerRuntime:
    config: McpServerConfig
    startup_timeout_s: float = 20.0
    call_timeout_s: float = 30.0
    status: str = "stopped"
    restart_count: int = 0
    last_error: str | None = None
    tool_defs: list[McpToolDef] = field(default_factory=list)
    _client: McpClient | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> list[McpToolDef]:
        async with self._lock:
            if self._client is not None and self._client.connected:
                return list(self.tool_defs)
            self.status = "starting"
            client = McpClient(call_timeout_s=self.call_timeout_s)
            try:
                async with asyncio.timeout(self.startup_timeout_s):
                    if self.config.transport == "stdio":
                        if not self.config.command:
                            raise ValueError(
                                f"mcp server '{self.config.name}': stdio requires command"
                            )
                        await client.connect_stdio(
                            self.config.command,
                            self.config.args,
                            self.config.env or None,
                        )
                    elif self.config.transport == "tcp":
                        await client.connect_tcp(self.config.host, self.config.port)
                    else:
                        raise ValueError(
                            f"mcp server '{self.config.name}': unknown transport "
                            f"'{self.config.transport}'"
                        )
                    tool_defs = await client.list_tools()
            except Exception as exc:
                await client.close()
                self.status = "failed"
                self.last_error = str(exc)
                raise
            old_client = self._client
            self._client = client
            self.tool_defs = tool_defs
            self.status = "healthy"
            self.last_error = None
            if old_client is not None:
                await old_client.close()
            return list(tool_defs)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self._client is None or not self._client.connected:
            self.restart_count += 1
            await self.start()
        assert self._client is not None
        try:
            return await self._client.call_tool(name, arguments)
        except McpServerUnavailableError as exc:
            self.status = "degraded"
            self.last_error = str(exc)
            self.restart_count += 1
            failed_client = self._client
            self._client = None
            if failed_client is not None:
                await failed_client.close()
            try:
                await self.start()
            except Exception:
                log.warning("mcp: restart failed server=%s", self.config.name, exc_info=True)
            raise McpServerUnavailableError(
                f"{exc}; connection recovered for future calls but current call was not replayed"
            ) from exc

    async def stop(self) -> None:
        async with self._lock:
            client = self._client
            self._client = None
            if client is not None:
                await client.close()
            self.status = "stopped"

    def health(self) -> dict[str, object]:
        return {
            "name": self.config.name,
            "transport": self.config.transport,
            "status": self.status,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "tool_count": len(self.tool_defs),
        }


class McpServerManager:
    def __init__(self, *, startup_timeout_s: float = 20.0, call_timeout_s: float = 30.0) -> None:
        self._servers: dict[str, McpServerRuntime] = {}
        self._tools: list[McpTool] = []
        self._startup_timeout_s = startup_timeout_s
        self._call_timeout_s = call_timeout_s

    async def start_all(self, servers: list[McpServerConfig]) -> None:
        if len({config.name for config in servers}) != len(servers):
            raise ValueError("MCP server names must be unique")
        runtimes = [
            McpServerRuntime(
                config,
                startup_timeout_s=self._startup_timeout_s,
                call_timeout_s=self._call_timeout_s,
            )
            for config in servers
        ]
        self._servers = {runtime.config.name: runtime for runtime in runtimes}
        results = await asyncio.gather(
            *(runtime.start() for runtime in runtimes),
            return_exceptions=True,
        )
        self._tools.clear()
        for runtime, result in zip(runtimes, results, strict=True):
            if isinstance(result, BaseException):
                log.error(
                    "mcp: server '%s' failed to start: %s",
                    runtime.config.name,
                    result,
                )
                continue
            self._tools.extend(
                McpTool(runtime, runtime.config.name, tool_def) for tool_def in result
            )
            log.info(
                "mcp: server '%s' connected, %d tool(s) discovered",
                runtime.config.name,
                len(result),
            )

    def register_tools(self, registry: ToolRegistry) -> None:
        for tool in self._tools:
            registry.register(tool)

    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    def health(self) -> list[dict[str, object]]:
        return [runtime.health() for runtime in self._servers.values()]

    async def stop_all(self) -> None:
        results = await asyncio.gather(
            *(runtime.stop() for runtime in self._servers.values()),
            return_exceptions=True,
        )
        for runtime, result in zip(self._servers.values(), results, strict=True):
            if isinstance(result, BaseException):
                log.warning("mcp: error closing server '%s': %s", runtime.config.name, result)
        self._servers.clear()
        self._tools.clear()
