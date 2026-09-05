from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.config import KamaConfig

_PID_FILE = Path.home() / ".kama" / "kama-core.pid"


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: KamaConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# PID 文件存 JSON：{"pid": int, "config": str|null}
# （旧版本只存裸 pid 数字，读取时做了兼容）
def _read_pid_file() -> dict[str, Any]:
    if not _PID_FILE.exists():
        return {}
    raw = _PID_FILE.read_text().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 兼容旧格式：纯数字
    try:
        return {"pid": int(raw), "config": None}
    except ValueError:
        return {}


def _write_pid_file(pid: int, config_path: str | None) -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(json.dumps({"pid": pid, "config": config_path}))


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    data = _read_pid_file()
    pid = data.get("pid")
    if not isinstance(pid, int):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return None


# 打印 daemon 当前状态（running / not running），附带其使用的配置文件
def cmd_core_status(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")
        return
    data = _read_pid_file()
    cfg = data.get("config")
    if cfg:
        print(f"config   {cfg}")


# 在后台启动 daemon，若已在运行则提示并退出
# config_path 为显式指定的配置文件路径，会以 --config 传给 daemon 子进程，
# 避免"KAMA_CONFIG=xxx kama-core"这种必须 export 才能生效的写法。
def cmd_core_start(config: KamaConfig, config_path: str | None = None) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    resolved = str(Path(config_path).expanduser()) if config_path else None

    argv = [sys.executable, "-m", "kama_claude.core"]
    if resolved:
        argv += ["--config", resolved]

    proc = subprocess.Popen(
        argv,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_pid_file(proc.pid, resolved)
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")
    if resolved:
        print(f"config   {resolved}")
    else:
        print("config   default lookup (KAMA_CONFIG or ~/.kama/config.toml)")


# 向 daemon 发送 SIGTERM 停止进程，若未运行则提示
def cmd_core_stop(config: KamaConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
