from __future__ import annotations

import argparse
import sys

from kama_claude.cli.commands.chat import cmd_chat
from kama_claude.cli.commands.core import cmd_core_start, cmd_core_status, cmd_core_stop
from kama_claude.cli.commands.eval import cmd_eval
from kama_claude.cli.commands.ping import cmd_ping
from kama_claude.cli.commands.run import cmd_run
from kama_claude.cli.commands.sessions import cmd_sessions
from kama_claude.cli.commands.trace import cmd_trace
from kama_claude.cli.commands.version import cmd_version
from kama_claude.core.config import get_config
from kama_claude.core.logging_setup import setup_logging


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="kama", description="KamaClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        help="配置文件路径（优先级高于 KAMA_CONFIG 环境变量，默认 ~/.kama/config.toml）",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")
    chat_parser = subparsers.add_parser("chat", help="Start or resume a chat session")
    resume_group = chat_parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", metavar="SESSION_ID", help="Resume a session")
    resume_group.add_argument("--last", action="store_true", help="Resume latest chat session")

    sessions_parser = subparsers.add_parser("sessions", help="List persisted sessions")
    sessions_parser.add_argument("--limit", type=int, default=20)

    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    eval_parser = subparsers.add_parser("eval", help="Evaluate a persisted run trajectory")
    eval_parser.add_argument("events_path", help="Path to a run events.jsonl file")

    core_parser = subparsers.add_parser("core", help="Manage the core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_start_parser = core_sub.add_parser("start", help="Start the daemon in the background")
    # dest 必须避开 args.config，否则 subparser 默认值会覆盖全局 --config
    core_start_parser.add_argument(
        "--config",
        "-c",
        dest="core_config",
        metavar="PATH",
        help="daemon 使用的配置文件路径（未指定时继承 kama 的 --config / KAMA_CONFIG / 默认路径）",
    )
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("status", help="Show daemon status")

    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config(args.config)
    setup_logging(config)

    if args.command == "ping":
        cmd_ping(config)
    elif args.command == "chat":
        cmd_chat(config, resume_session_id=args.resume, resume_last=args.last)
    elif args.command == "sessions":
        cmd_sessions(config, args.limit)
    elif args.command == "run":
        cmd_run(args.goal, config)
    elif args.command == "eval":
        cmd_eval(args.events_path)
    elif args.command == "core":
        if args.core_command == "start":
            cmd_core_start(config, config_path=getattr(args, "core_config", None) or args.config)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "status":
            cmd_core_status(config)
        else:
            core_parser.print_help()
            sys.exit(1)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    else:
        parser.print_help()
        sys.exit(1)
