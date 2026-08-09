from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.context import ExecutionContext


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SubagentRecord:
    run_id: str
    parent_run_id: str
    session_id: str
    description: str
    status: str
    created_at: str
    updated_at: str
    result: str = ""
    reason: str | None = None
    depth: int = 0


@dataclass
class _Entry:
    task: asyncio.Task[None]
    context: ExecutionContext
    record: SubagentRecord


class BackgroundTaskRegistry:
    """Core-scoped subagent supervisor with durable terminal records."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_concurrency: int = 4,
        max_children_per_parent: int = 8,
    ) -> None:
        self._root = root.expanduser() if root is not None else None
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
        self._max_children_per_parent = max_children_per_parent
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._entries: dict[str, _Entry] = {}
        self._records: dict[str, SubagentRecord] = {}
        self._load_records()

    def _load_records(self) -> None:
        if self._root is None:
            return
        for path in self._root.glob("*.json"):
            try:
                record = SubagentRecord(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if record.status in {"pending", "running"}:
                record.status = "interrupted"
                record.reason = "daemon_restarted"
                record.updated_at = _now()
                self._persist(record)
            self._records[record.run_id] = record

    def _persist(self, record: SubagentRecord) -> None:
        if self._root is None:
            return
        target = self._root / f"{record.run_id}.json"
        temp = self._root / f".{record.run_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(asdict(record), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def spawn(
        self,
        *,
        run_id: str,
        parent_run_id: str,
        session_id: str,
        description: str,
        depth: int,
        context: ExecutionContext,
        work: Callable[[], Awaitable[None]],
        timeout_s: float,
    ) -> asyncio.Task[None]:
        child_count = sum(
            1 for record in self._records.values() if record.parent_run_id == parent_run_id
        )
        if child_count >= self._max_children_per_parent:
            raise RuntimeError(
                f"subagent child limit reached parent={parent_run_id} "
                f"limit={self._max_children_per_parent}"
            )
        timestamp = _now()
        record = SubagentRecord(
            run_id=run_id,
            parent_run_id=parent_run_id,
            session_id=session_id,
            description=description,
            status="pending",
            created_at=timestamp,
            updated_at=timestamp,
            depth=depth,
        )
        self._records[run_id] = record
        self._persist(record)

        async def supervised() -> None:
            try:
                async with self._semaphore:
                    record.status = "running"
                    record.updated_at = _now()
                    self._persist(record)
                    async with asyncio.timeout(timeout_s):
                        await work()
                    record.status = context.status
                    record.result = context.result
                    record.reason = context.reason
            except TimeoutError:
                if not context.is_done():
                    context.mark_failed("subagent_timeout")
                record.status = "timed_out"
                record.reason = "subagent_timeout"
            except asyncio.CancelledError:
                if not context.is_done():
                    context.mark_failed("cancelled")
                record.status = "cancelled"
                record.reason = "cancelled"
                raise
            except Exception as exc:
                if not context.is_done():
                    context.mark_failed("subagent_error")
                record.status = "failed"
                record.reason = f"{type(exc).__name__}: {exc}"
            finally:
                record.result = context.result
                record.updated_at = _now()
                self._persist(record)

        task = asyncio.create_task(supervised(), name=f"subagent:{run_id}")
        self._entries[run_id] = _Entry(task=task, context=context, record=record)
        task.add_done_callback(lambda _task: self._entries.pop(run_id, None))
        return task

    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
    ) -> None:
        timestamp = _now()
        record = SubagentRecord(
            run_id=run_id,
            parent_run_id="",
            session_id="",
            description="",
            status="running",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._records[run_id] = record
        self._entries[run_id] = _Entry(task, context, record)

    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        entry = self._entries.get(run_id)
        return (entry.task, entry.context) if entry is not None else None

    def get_record(self, run_id: str) -> SubagentRecord | None:
        return self._records.get(run_id)

    async def wait(self, run_id: str, timeout_s: float) -> SubagentRecord | None:
        entry = self._entries.get(run_id)
        if entry is not None:
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout_s)
            except TimeoutError:
                return entry.record
            except asyncio.CancelledError:
                if not entry.task.cancelled():
                    raise
        return self._records.get(run_id)

    async def cancel(self, run_id: str) -> bool:
        entry = self._entries.get(run_id)
        if entry is None:
            return False
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        return True

    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return [(entry.task, entry.context) for entry in self._entries.values()]

    async def shutdown(self) -> None:
        tasks = [entry.task for entry in self._entries.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
