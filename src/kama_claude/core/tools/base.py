from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "conflict"
    error_type: str | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: type[BaseModel] | None = None
    # Planning safety and concurrency are different contracts. A read-only
    # remote tool may still require serialized access to its server.
    read_only: bool = False
    parallel_safe: bool = False

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
