from __future__ import annotations

from typing import Any

from rich.markup import escape as _esc


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 从 session.list 结果中筛出可恢复的 chat 会话：排除当前会话，按 updated_at 倒序
def _filter_resumable_sessions(
    sessions: list[dict[str, Any]], current_id: str | None
) -> list[dict[str, Any]]:
    chat = [
        s for s in sessions
        if s.get("mode") == "chat" and s.get("session_id") != current_id
    ]
    chat.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return chat


# 把 /resume 的参数解析为 session_id：纯数字按最近列表序号（1-based）解析，否则原样作为 id
def _resolve_resume_target(arg: str, recent: list[dict[str, Any]]) -> str | None:
    if not arg:
        return None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(recent):
            return str(recent[idx].get("session_id"))
        return None
    return arg


# 把 tool_result 的 content（可能是字符串或 [{type:text,...}]）压平成纯文本
def flatten_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


# 把 Anthropic 格式的会话历史解析为有序的「可渲染条目」列表，供 TUI 渲染 & 单测。
# 每个条目：user_text / assistant_text / tool_use / tool_result，tool_use 与 tool_result
# 通过 tool_use_id 在渲染阶段配对。
def parse_history_for_render(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                items.append({"kind": "user_text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type")
                    if bt == "text":
                        t = block.get("text", "")
                        if t:
                            items.append({"kind": "user_text", "text": t})
                    elif bt == "tool_result":
                        items.append({
                            "kind": "tool_result",
                            "tool_use_id": str(block.get("tool_use_id", "")),
                            "content": flatten_tool_result_content(block.get("content", "")),
                            "is_error": bool(block.get("is_error", False)),
                        })
        elif role == "assistant":
            if isinstance(content, str):
                items.append({"kind": "assistant_text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type")
                    if bt == "text":
                        t = block.get("text", "")
                        if t:
                            items.append({"kind": "assistant_text", "text": t})
                    elif bt == "tool_use":
                        items.append({
                            "kind": "tool_use",
                            "tool_use_id": str(block.get("id", "")),
                            "name": block.get("name", "tool"),
                            "input": block.get("input") or {},
                        })
    return items


# 状态 -> (友好标签, rich 颜色)，用于在 TUI 列表里把 raw 状态翻成人话
_STATUS_LABELS: dict[str, tuple[str, str]] = {
    "active": ("empty", "yellow"),
    "running": ("running", "yellow"),
    "waiting_for_input": ("ready", "green"),
    "interrupted": ("interrupted", "red"),
    "closed": ("closed", "dim"),
}


def _status_label(status: str) -> tuple[str, str]:
    return _STATUS_LABELS.get(status, (status, "dim"))


# 压缩（手动 /compact 或运行时自动压缩）会把 thread 重写为 [user:摘要, assistant:ack]，
# 其中 ack 文本固定为下方常量。据此可检测一段历史是否为「压缩后」版本。
_COMPACT_ACK = "Understood, I'll continue from this summary."


def is_compacted_history(messages: list[dict[str, Any]]) -> bool:
    if len(messages) < 2:
        return False
    second = messages[1]
    if second.get("role") != "assistant":
        return False
    content = second.get("content", "")
    if isinstance(content, str):
        return _COMPACT_ACK in content
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and _COMPACT_ACK in block.get("text", "")
            ):
                return True
    return False


# 把会话列表渲染为可显示的编号文本（含友好状态标签、历史标记、状态图例）
def _render_session_list(recent: list[dict[str, Any]]) -> str:
    lines = [
        "[bold]recent chat sessions[/bold]  "
        "[dim](use /resume <id> or /resume <n>)[/dim]"
    ]
    for i, s in enumerate(recent, start=1):
        reason = (
            f" ({_esc(str(s.get('interrupted_reason', '')))})"
            if s.get("interrupted_reason")
            else ""
        )
        label, color = _status_label(s.get("status", ""))
        runs = s.get("run_count", 0)
        history = "[dim](空)[/dim]" if not runs else f"[dim](有历史 {runs} 轮)[/dim]"
        title = _esc(_preview(str(s.get("title", "") or "(no title)"), 50))
        sid = _esc(str(s.get("session_id", "")))
        lines.append(
            f"  [cyan]{i}.[/cyan] {sid}  "
            f"[{color}]{label}[/{color}]{reason}  {history}  {title}"
        )
    lines.append(
        "[dim]状态: ready=有历史可续 · empty=空会话 · interrupted=被打断 · closed=已关闭[/dim]"
    )
    return "\n".join(lines)
