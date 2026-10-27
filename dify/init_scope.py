"""为《记忆酒馆》创建 PowerContext scope，并把 scope_id 写到 scope.txt。"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
SCOPE_FILE = Path(__file__).resolve().parent / "scope.txt"


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def main() -> int:
    # 幂等：同一个 idempotency_key 重复调用只会返回同一个 scope
    try:
        created = post("/v1/scopes", {
            "title": "Memory Tavern",
            "summary": "memory tavern game scope",
            "idempotency_key": "tavern-v1",
        })
        scope_id = created.get("scope_id") or ""
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        print(f"创建 scope 失败: {error.code} {body}")
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"连接 PowerContext 失败: {type(error).__name__}: {error}")
        print("请确认 PowerContext Server 已在 http://127.0.0.1:8000 运行")
        return 1

    if not scope_id:
        print(f"响应里没有 scope_id: {created}")
        return 1

    SCOPE_FILE.write_text(scope_id, encoding="utf-8")
    print(f"scope_id = {scope_id}")
    print(f"已写入 {SCOPE_FILE}")
    print("接下来：把 Dify DSL 里两处 <SCOPE_ID> 替换成这个值（search_memory 与 remember_deed 节点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
