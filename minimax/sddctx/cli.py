"""SDD 上下文模块 CLI。

子命令：
  index    把业务工程的规格/代码/测试导入 seekdb
  build    针对一个任务构造上下文（规格 + 代码 + 测试 + 任务状态）
  state    读写 PowerContext 中的任务状态
  check    自检三项技术的连通性
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import SeekdbIndex, TaskState, build_context, collect

DEFAULT_PROJECT = Path(__file__).resolve().parent.parent / "demo_project"


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    docs = collect(root)
    if not docs:
        print(f"no documents found under {root}", file=sys.stderr)
        return 1
    count = SeekdbIndex().replace_all(docs)
    kinds: dict[str, int] = {}
    for doc in docs:
        kinds[doc.kind] = kinds.get(doc.kind, 0) + 1
    print(json.dumps({"indexed": count, "by_kind": kinds}, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    index = SeekdbIndex()
    state = TaskState()
    ctx = build_context(index, state, args.task, limit=args.limit)
    print(json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    state = TaskState()
    if args.set:
        key, value = args.set, args.value or ""
        result = state.save(key, json.loads(value) if value.startswith(("[", "{")) else value)
        print(json.dumps({"saved": {key: value}, "result": result}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(state.load(), ensure_ascii=False, indent=2))
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    report: dict[str, object] = {}

    try:
        index = SeekdbIndex()
        index.connect()
        docs = index.by_kind("spec")
        report["seekdb"] = {"ok": True, "specs": len(docs), "database": index.database}
    except Exception as error:  # noqa: BLE001
        report["seekdb"] = {"ok": False, "error": f"{type(error).__name__}: {error}"}

    try:
        state = TaskState()
        scope = state.ensure_scope()
        report["powercontext"] = {"ok": bool(scope), "scope": scope}
    except Exception as error:  # noqa: BLE001
        report["powercontext"] = {"ok": False, "error": f"{type(error).__name__}: {error}"}

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(v.get("ok") for v in report.values() if isinstance(v, dict)) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sddctx", description="SDD 上下文模块")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="导入工程资料到 seekdb")
    p_index.add_argument("--project", default=str(DEFAULT_PROJECT))
    p_index.set_defaults(func=cmd_index)

    p_build = sub.add_parser("build", help="构造任务上下文")
    p_build.add_argument("--task", required=True)
    p_build.add_argument("--limit", type=int, default=6)
    p_build.set_defaults(func=cmd_build)

    p_state = sub.add_parser("state", help="读取或写入任务状态")
    p_state.add_argument("--set", choices=["completed", "todo", "verification", "context"])
    p_state.add_argument("--value")
    p_state.set_defaults(func=cmd_state)

    p_check = sub.add_parser("check", help="自检 seekdb 与 PowerContext")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
