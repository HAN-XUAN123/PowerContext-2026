"""用 MiniMax 完成本次代码修改，并记录完整的开发过程。

流程：
1. 调用 sddctx 构造任务上下文（规格 + 代码 + 测试 + 任务状态）
2. 把上下文拼成 prompt，调用 MiniMax Chat Completions
3. 从回复里提取修改后的 pricing.py，写回业务工程（保留 .orig 备份）
4. 把请求、响应、最终 diff 写入 docs/dev-records/
5. 可选：跑测试并把结果写进 PowerContext 任务状态

用法：
    python agent/minimax_patch.py --task "..." [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sddctx.core import SeekdbIndex, TaskState, build_context  # noqa: E402

DEFAULT_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
DEFAULT_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")

SYSTEM_PROMPT = """你是遵循规格驱动开发（SDD）的工程师。你只会收到：本任务的规格条目、需要修改的代码、验证测试。
请严格按规格修改代码，不要改动测试，不要引入新依赖，不要改变与规格无关的行为。
只输出一个 ```python 代码块，内容是修改后的完整 pricing.py 文件。"""


def render_prompt(ctx) -> str:
    parts = ["# 任务", ctx.task, "", "# 必须遵守的规格"]
    for spec in ctx.specs:
        parts.append(f"## {spec['id']} — {spec['title']}\n{spec['content']}")
    parts.append("\n# 需要修改的代码")
    for code in ctx.code:
        parts.append(f"## {code['path']}（符号：{code['symbols']}）\n```python\n{code['source']}\n```")
    parts.append("\n# 验证测试（不得修改）")
    for test in ctx.tests:
        parts.append(f"## {test['path']}\n```python\n{test['source']}\n```")
    parts.append("\n请输出修改后的完整 pricing.py。")
    return "\n".join(parts)


def call_minimax(prompt: str, base: str, model: str, api_key: str, timeout: int = 180) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/text/chatcompletion_v2",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_code(text: str) -> str:
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if not blocks:
        raise ValueError("回复中没有 python 代码块")
    code = max(blocks, key=len).rstrip() + "\n"
    if "def apply_discount" not in code:
        raise ValueError("提取到的代码不含 apply_discount")
    return code


def dry_run_reply() -> dict:
    """离线占位：把规格 003 的实现补上，用于验证整条流水线（不产生 MiniMax 记录）。"""
    path = ROOT / "demo_project" / "pricing.py"
    code = path.read_text(encoding="utf-8")
    code = code.replace(
        "    # TODO(SPEC-DISCOUNT-003): 缺少上限校验",
        "    if discount_rate > VIP_MAX_DISCOUNT:\n"
        "        raise ValueError(\"discount rate exceeds limit: %s\" % discount_rate)",
    )
    return {"choices": [{"message": {"content": "```python\n" + code + "```"}}], "dry_run": True}


def run_tests() -> dict:
    python = sys.executable
    proc = subprocess.run(
        [python, "-m", "pytest", "tests/test_pricing.py", "-q", "--no-header"],
        cwd=str(ROOT / "demo_project"), capture_output=True, text=True, timeout=300,
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 MiniMax 完成 SDD 任务的代码修改")
    parser.add_argument("--task", required=True, help="任务描述")
    parser.add_argument("--dry-run", action="store_true", help="不调用 MiniMax，用离线补丁验证流水线")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    records = ROOT / "docs" / "dev-records"
    records.mkdir(parents=True, exist_ok=True)

    # 1) 构造上下文
    ctx = build_context(SeekdbIndex(), TaskState(), args.task)
    prompt = render_prompt(ctx)
    (records / f"{stamp}-context.json").write_text(
        json.dumps(ctx.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 调 MiniMax
    if args.dry_run:
        reply = dry_run_reply()
        meta = {"mode": "dry-run"}
    else:
        api_key = os.environ.get("MINIMAX_API_KEY", "")
        if not api_key:
            print("缺少 MINIMAX_API_KEY 环境变量", file=sys.stderr)
            return 2
        base = os.environ.get("MINIMAX_BASE_URL", DEFAULT_BASE)
        model = os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
        reply = call_minimax(prompt, base, model, api_key)
        meta = {"mode": "minimax", "base": base, "model": model}

    content = ""
    try:
        content = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("MiniMax 回复结构异常: " + json.dumps(reply, ensure_ascii=False)[:500], file=sys.stderr)
        return 3

    (records / f"{stamp}-prompt.md").write_text(prompt, encoding="utf-8")
    (records / f"{stamp}-response.json").write_text(
        json.dumps({"meta": meta, "reply": reply}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) 落盘（保留原文件）
    target = ROOT / "demo_project" / "pricing.py"
    shutil.copyfile(target, records / f"{stamp}-pricing.py.before")
    code = extract_code(content)
    target.write_text(code, encoding="utf-8")
    shutil.copyfile(target, records / f"{stamp}-pricing.py.after")

    # 4) 测试 + 记录
    result: dict = {}
    if not args.skip_tests:
        result = run_tests()
        (ROOT / "docs" / "logs").mkdir(parents=True, exist_ok=True)
        (ROOT / "docs" / "logs" / f"{stamp}-pytest.log").write_text(
            f"$ {sys.executable} -m pytest tests/test_pricing.py -q\n{result['stdout']}\n"
            f"exit code: {result['exit_code']}\n", encoding="utf-8")

    # 5) 状态写回 PowerContext
    state = TaskState()
    state.save("completed", f"{stamp} MiniMax 生成修改并通过测试" if result.get("exit_code") == 0
               else f"{stamp} MiniMax 生成修改，测试未通过")
    state.save("verification", f"{stamp} pytest exit={result.get('exit_code')}")
    state.save("context", {"task": args.task, "specs": [s["id"] for s in ctx.specs],
                           "code": [c["path"] for c in ctx.code],
                           "tests": [t["path"] for t in ctx.tests]})

    print(json.dumps({
        "mode": meta["mode"],
        "context": {"specs": len(ctx.specs), "code": len(ctx.code), "tests": len(ctx.tests)},
        "test_exit_code": result.get("exit_code"),
        "records": str(records),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("exit_code", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
