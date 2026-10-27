"""端到端验证脚本：正确使用 UTF-8，避开 PowerShell 控制台编码问题。

用法（窗口 D）：
    cd D:\projects\qoder-paperlens
    .\.venv\Scripts\python.exe verify_client.py
"""

import json
import urllib.request

BASE = "http://127.0.0.1:8077"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"http_error": error.code, "body": error.read().decode("utf-8", "replace")}


def show(title, payload):
    print("\n=== " + title + " ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


show("1) health", get("/health"))
show("2) remember", post("/memory/preference", {"topic": "混合检索", "progress": "读到第3章"}))
show("3) recall preference", get("/memory/preference"))

from urllib.parse import quote

show("4) hybrid search", get("/search?q=" + quote("向量与全文怎么融合") + "&top_k=3"))
