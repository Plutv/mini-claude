from __future__ import annotations

from typing import Any, Protocol

from kama_claude.core.mcp.client import McpServerUnavailableError, McpToolDef, McpToolError
from kama_claude.core.tools.base import BaseTool, ToolResult


class McpToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str: ...


class McpTool(BaseTool):
    """Expose an MCP tool through the local ToolRegistry interface."""

    params_model = None

    def __init__(self, client: McpToolCaller, server_name: str, tool_def: McpToolDef) -> None:
        self._client = client
        self._server_name = server_name
        self._tool_def = tool_def
        self.name = f"{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = (
            tool_def.input_schema or {"type": "object", "properties": {}}
        )
        self.read_only = tool_def.read_only_hint
        # MCP readOnlyHint describes side effects, not whether the remote server
        # safely supports concurrent calls. Keep remote calls serialized unless
        # a future capability explicitly declares concurrency safety.
        self.parallel_safe = False

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            content = await self._client.call_tool(self._tool_def.name, dict(params))
            return ToolResult(content=content)
        except McpServerUnavailableError as exc:
            return ToolResult(
                content=f"mcp server '{self._server_name}' unavailable: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except McpToolError as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except Exception as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' unexpected error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
