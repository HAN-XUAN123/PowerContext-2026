# 验证样例（可按步骤执行）

> 目标：用一条可复现的路径，证明 seekdb 与 PowerContext 在项目里**实际发挥作用**。
> 全部命令可在本机完成，无需外部网络。

## 0. 前置

```bash
# seekdb（二选一）
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
# 或按 seekdb 文档启动服务端，确认 2881 端口可连

# PowerContext
# 按官方文档启动 HTTP 服务，确认 http://127.0.0.1:8000 可访问

cd qoder-paperlens
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SEEKD_DB_HOST=127.0.0.1 SEEKD_DB_PORT=2881 SEEKD_DB_USER=root SEEKD_DB_PASSWORD=
export SEEKD_DB_NAME=paperlens
export POWERCX_BASE_URL=http://127.0.0.1:8000 POWERCX_SCOPE=paperlens-demo
```

## 1. 初始数据

```bash
python -m src.app --load-sample
```

预期输出：

```json
{"documents": 5, "chunks": 7}
```

（5 篇示例资料，按段落切块后 7 块；数字随内容长度略有浮动。）

## 2. 依赖自检

```bash
curl -s http://127.0.0.1:8077/health | python -m json.tool
```

预期：`seekdb.ok` 与 `powercontext.ok` 均为 `true`。

> 若 PowerContext 未启动，`/search` 仍可用（`memory_used=false`），但无法演示记忆复用。

## 3. 写入偏好（PowerContext 生效）

```bash
curl -s -X POST http://127.0.0.1:8077/memory/preference \
  -H 'Content-Type: application/json' \
  -d '{"topic":"混合检索","progress":"读到第 3 章"}'
```

预期：返回 `written` 中有两条记忆写入结果，`scope` 为 `paperlens-demo`。

```bash
curl -s http://127.0.0.1:8077/memory/preference | python -m json.tool
```

预期：`preference.preferred_topic` 为 `混合检索`，`preference.progress` 为 `读到第 3 章`。

## 4. 混合检索（seekdb + 记忆复用同时生效）

```bash
curl -s "http://127.0.0.1:8077/search?q=向量与全文怎么融合&top_k=3" | python -m json.tool
```

预期：

| 检查项 | 预期 |
|---|---|
| `memory_used` | `true` |
| `preference.preferred_topic` | `混合检索` |
| `results` 数量 | 3 |
| 第 1 条 `doc_id` | `paper-hybrid-retrieval` |
| 每条 `via` | `fulltext+vector` 或 `vector` / `fulltext` |

## 5. 证明 topic 加权真的起作用

对比两次检索（第二次显式关闭 topic）：

```bash
curl -s "http://127.0.0.1:8077/search?q=切块与检索质量&top_k=3" | python -m json.tool
curl -s "http://127.0.0.1:8077/search?q=切块与检索质量&top_k=3&topic=混合检索" | python -m json.tool
```

预期：第二次里 `topic=混合检索` 的块排序被提升（`score` 更大），因为记忆偏好带来了 `topic_boost`。

## 6. 证明 seekdb 是唯一数据来源

```bash
python - <<'PY'
import os, pymysql
conn = pymysql.connect(host=os.environ.get("SEEKD_DB_HOST","127.0.0.1"),
                       port=int(os.environ.get("SEEKD_DB_PORT","2881")),
                       user="root", password="", charset="utf8mb4")
cur = conn.cursor()
cur.execute("USE paperlens")
cur.execute("SELECT COUNT(*) FROM chunks")
print("chunks =", cur.fetchone()[0])
cur.execute("SELECT doc_id, topic, LEFT(body, 40) FROM chunks LIMIT 3")
for row in cur.fetchall():
    print(row)
PY
```

预期：能直接读到 `chunks` 表内容，说明检索结果确实来自 seekdb。

## 7. 断言脚本（可选，便于自动化核对）

```bash
python - <<'PY'
import json, urllib.request

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8077" + path) as r:
        return json.loads(r.read())

data = get("/search?q=向量与全文怎么融合&top_k=3")
assert data["memory_used"] is True, data
assert data["preference"]["preferred_topic"] == "混合检索", data
assert len(data["results"]) == 3, data
print("OK:", data["results"][0]["doc_id"], data["results"][0]["via"])
PY
```

## 8. 截图建议

1. `/health` 两个 `ok: true`
2. `/memory/preference` 写入与读回
3. `/search` 返回 `memory_used: true` 与 `preference`
4. seekdb 中 `chunks` 表的查询结果
5. Qoder 开发记录（见 `docs/qoder-dev-log.md`）
