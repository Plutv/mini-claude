from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kama_claude.core.context import ExecutionContext
from kama_claude.core.subagent.registry import BackgroundTaskRegistry


def _context(run_id: str) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, goal="child", max_steps=2)


async def test_terminal_result_survives_new_registry_instance(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(tmp_path)
    context = _context("child-1")

    async def work() -> None:
        context.result = "durable result"
        context.mark_success()

    task = registry.spawn(
        run_id="child-1",
        parent_run_id="parent-1",
        session_id="session-1",
        description="durable child",
        depth=1,
        context=context,
        work=work,
        timeout_s=1,
    )
    await task

    restored = BackgroundTaskRegistry(tmp_path).get_record("child-1")

    assert restored is not None
    assert restored.status == "success"
    assert restored.result == "durable result"


def test_running_record_becomes_interrupted_after_restart(tmp_path: Path) -> None:
    (tmp_path / "child.json").write_text(
        json.dumps(
            {
                "run_id": "child",
                "parent_run_id": "parent",
                "session_id": "session",
                "description": "work",
                "status": "running",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "result": "",
                "reason": None,
                "depth": 1,
            }
        ),
        encoding="utf-8",
    )

    record = BackgroundTaskRegistry(tmp_path).get_record("child")

    assert record is not None
    assert record.status == "interrupted"
    assert record.reason == "daemon_restarted"


async def test_registry_enforces_global_concurrency_limit(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(tmp_path, max_concurrency=2)
    active = 0
    maximum = 0
    release = asyncio.Event()

    def spawn(index: int) -> asyncio.Task[None]:
        context = _context(f"child-{index}")

        async def work() -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await release.wait()
            active -= 1
            context.mark_success()

        return registry.spawn(
            run_id=f"child-{index}",
            parent_run_id="parent",
            session_id="session",
            description="work",
            depth=1,
            context=context,
            work=work,
            timeout_s=1,
        )

    tasks = [spawn(index) for index in range(3)]
    await asyncio.sleep(0.02)
    assert maximum == 2
    release.set()
    await asyncio.gather(*tasks)


async def test_wait_timeout_does_not_cancel_child_and_explicit_cancel_does(
    tmp_path: Path,
) -> None:
    registry = BackgroundTaskRegistry(tmp_path)
    context = _context("child")
    block = asyncio.Event()

    async def work() -> None:
        await block.wait()

    registry.spawn(
        run_id="child",
        parent_run_id="parent",
        session_id="session",
        description="work",
        depth=1,
        context=context,
        work=work,
        timeout_s=30,
    )

    record = await registry.wait("child", timeout_s=0.01)
    assert record is not None
    assert record.status == "running"
    assert await registry.cancel("child") is True
    cancelled = registry.get_record("child")
    assert cancelled is not None
    assert cancelled.status == "cancelled"


async def test_registry_enforces_child_limit_per_parent(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(tmp_path, max_children_per_parent=1)
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    registry.spawn(
        run_id="child-1",
        parent_run_id="parent",
        session_id="session",
        description="first",
        depth=1,
        context=_context("child-1"),
        work=work,
        timeout_s=30,
    )

    try:
        registry.spawn(
            run_id="child-2",
            parent_run_id="parent",
            session_id="session",
            description="second",
            depth=1,
            context=_context("child-2"),
            work=work,
            timeout_s=30,
        )
    except RuntimeError as exc:
        assert "child limit reached" in str(exc)
    else:
        raise AssertionError("expected child limit to reject the second subagent")
    await registry.shutdown()
