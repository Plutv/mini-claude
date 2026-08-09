from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import BaseTool, ToolResult


class NoteSaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str
    scope: str = "session"
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class NoteSaveTool(BaseTool):
    params_model = NoteSaveParams
    name = "note_save"
    description = (
        "Save a concise durable fact, user preference, or project decision to long-term memory. "
        "Choose session, project, or global scope; do not save transient tool output."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The durable fact or decision to remember.",
            },
            "scope": {
                "type": "string",
                "enum": ["session", "project", "global"],
                "description": "Where this memory should be available.",
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["content"],
    }

    # 绑定当前 session 与 run，使工具调用能写入对应 notes.md
    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        run_id: str,
        *,
        memory_store: MemoryStore | None = None,
        project_scope: str = "",
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._memory_store = memory_store
        self._project_scope = project_scope

    # 将非空 content 追加到 session notes.md
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = NoteSaveParams.model_validate(params)
        content = parsed.content.strip()
        if not content:
            return ToolResult(
                content="empty content",
                is_error=True,
                error_type="runtime_error",
            )
        if parsed.scope not in {"session", "project", "global"}:
            return ToolResult(
                content="scope must be session, project, or global",
                is_error=True,
                error_type="schema_error",
            )
        scope = {
            "session": f"session:{self._session_id}",
            "project": self._project_scope,
            "global": "global",
        }[parsed.scope]
        if parsed.scope == "project" and not scope:
            return ToolResult(
                content="project scope unavailable",
                is_error=True,
                error_type="runtime_error",
            )
        if self._memory_store is not None:
            record = await asyncio.to_thread(
                self._memory_store.save,
                scope=scope,
                content=content,
                tags=parsed.tags,
                importance=parsed.importance,
                source_run_id=self._run_id,
            )
            return ToolResult(content=f"saved memory {record.id} scope={parsed.scope}")
        self._store.append_note(self._session_id, content, self._run_id)
        return ToolResult(content="saved")
