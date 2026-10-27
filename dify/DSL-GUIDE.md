# 《记忆酒馆》Dify Chatflow 搭建指南（不需要任何模型 API）

> 目标：在 Dify 云端用 **Chatflow** 搭一个会记住玩家行为的文字小游戏。
> 全程只用 Start / HTTP Request / Code / Answer 四种节点，**不调用任何 LLM**，因此不需要 OpenAI 之类的 Key。

---

## 0. 准备 PowerContext

四个终端窗口里，窗口 C 跑着 PowerContext（8000 端口）。先在窗口 D 执行，创建游戏用的 scope：

```powershell
cd D:\projects\qoder-paperlens
.\.venv\Scripts\python.exe -c "import json,urllib.request as u; req=u.Request('http://127.0.0.1:8000/v1/scopes', data=json.dumps({'title':'Memory Tavern','summary':'memory tavern game','idempotency_key':'tavern-v1'}).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(json.load(u.urlopen(req)))"
```

记下返回的 `scope_id`（形如 `scp_xxxxxxxx`），后面两个 HTTP 节点都要用。

> ⚠️ Dify 云端运行在公网，**访问不到你本机的 127.0.0.1:8000**。二选一：
> - **本地 Dify**（Docker）→ 可以用 `http://host.docker.internal:8000` 访问本机 PowerContext；
> - **云端 Dify** → 需要把 PowerContext 暴露到公网（内网穿透），或者改用云端能访问的地址。
> 建议用**本地 Dify**，最省事。

---

## 1. 新建应用

Dify → 创建应用 → **Chatflow** → 名称「记忆酒馆」→ 创建。

进入编排界面后，会看到默认的「开始」和「直接回复」节点。删掉「直接回复」，保留「开始」。

---

## 2. 节点 1：开始（Start）

- 添加一个变量：变量名 `action`，类型 **文本**，必填，最大长度 200
- 描述：`玩家这一轮的输入，例如"来杯最烈的酒"`

---

## 3. 节点 2：查记忆（HTTP Request）

连接到「开始」之后。配置：

| 项 | 值 |
|---|---|
| 方法 | POST |
| URL | `http://host.docker.internal:8000/v1/memory/search` |
| Headers | `Content-Type: application/json` |
| Body 类型 | JSON |
| Body | 见下方 |

Body：

```json
{
  "scope_id": "<你的 scope_id>",
  "query": "{{#start.action#}}",
  "limit": 10
}
```

**输出变量**：勾选 `body`、`status_code`。

节点命名：`search_memory`

---

## 4. 节点 3：酒馆逻辑（Code）

连接到「查记忆」之后。语言选 **Python3**，输入变量：

| 变量名 | 值 |
|---|---|
| `action` | `{{#start.action#}}` |
| `memory` | `{{#search_memory.body#}}` |

代码（直接粘贴）：

```python
import json

def main(action: str, memory: str) -> dict:
    # 1) 解析历史记忆
    deeds = []
    try:
        data = json.loads(memory) if isinstance(memory, str) else (memory or {})
        for hit in (data.get("hits") or []):
            text = hit.get("text") or ""
            if text.startswith("deed:"):
                deeds.append(text.split(":", 1)[1].strip())
    except Exception:
        deeds = []

    act = (action or "").strip()

    # 2) 关键词 -> 老板反应（模板化，无需 LLM）
    if any(k in act for k in ["酒", "喝", "杯"]):
        deed, line = "点了一杯酒", "老板擦着杯子：「来了？还是老样子。」"
    elif any(k in act for k in ["打", "砸", "掀", "闹"]):
        deed, line = "在店里闹事", "老板眯起眼：「又来？上次的账还没结。」"
    elif any(k in act for k in ["钱", "赔", "还"]):
        deed, line = "提到赔钱", "老板哼了一声：「算你还有点良心。」"
    elif any(k in act for k in ["走", "再见", "离开"]):
        deed, line = "准备离开", "老板头也不抬：「下次记得带钱。」"
    else:
        deed, line = f"说了一句「{act[:40]}」", "老板抬眼看了看你，没接话。"

    # 3) 记忆复用：如果以前闹过事，这轮回应要提到
    history_hint = ""
    if any("闹事" in d for d in deeds) and "闹事" not in deed:
        history_hint = "（老板显然还记得你上次闹的事。）"
    elif deeds:
        history_hint = f"（老板记得你之前{deeds[-1]}。）"

    tavern_state = "打烊中" if "走" in act or "离开" in act else "营业中"

    return {
        "reply": line + history_hint,
        "deed": deed,
        "memory_count": len(deeds),
        "state": tavern_state,
    }
```

**输出变量**：`reply`、`deed`、`memory_count`、`state`

节点命名：`tavern_logic`

---

## 5. 节点 4：写记忆（HTTP Request）

连接到「酒馆逻辑」之后。

| 项 | 值 |
|---|---|
| 方法 | POST |
| URL | `http://host.docker.internal:8000/v1/memory/remember` |
| Headers | `Content-Type: application/json` |
| Body | 见下方 |

```json
{
  "scope_id": "<你的 scope_id>",
  "kind": "deed",
  "text": "deed: {{#tavern_logic.deed#}}"
}
```

节点命名：`remember_deed`

---

## 6. 节点 5：回答（Answer）

连接到「写记忆」之后。内容：

```
{{#tavern_logic.reply#}}

—— 酒馆{{#tavern_logic.state#}}｜老板记得你 {{#tavern_logic.memory_count#}} 件事
```

---

## 7. 连线顺序

```
开始 → search_memory → tavern_logic → remember_deed → 回答
```

---

## 8. 自测三轮（验证记忆真的生效）

在同一会话里依次发：

| 轮次 | 输入 | 预期 |
|---|---|---|
| 1 | `来杯最烈的酒` | 「来了？还是老样子。」，记得你 0 件事 |
| 2 | `我把桌子掀了` | 「又来？上次的账还没结。」，记得你 1 件事 |
| 3 | `随便说点什么` | 出现「（老板显然还记得你上次闹的事。）」，记得你 2 件事 |

**关键验证**：新开一个对话（新会话），第一句发 `来杯酒`，如果回复里出现「（老板记得你之前…闹事…）」，说明**跨会话记忆生效**——这正是 PowerContext 在起作用。

---

## 9. 导出 DSL

Dify → 应用右上角 `...` → **导出 DSL** → 保存为 `memory-tavern.dsl.yml`，放进仓库 `dify/` 目录。

---

## 10. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| HTTP 节点报连接失败 | 云端 Dify 访问不到 127.0.0.1 | 用本地 Dify + `host.docker.internal`，或内网穿透 |
| code 节点报 `action` 未定义 | Start 变量名不是 `action` | 检查开始节点变量名 |
| 记忆数一直是 0 | `kind` 不是 `deed` 或 scope 不一致 | 两个 HTTP 节点用同一个 scope_id |
| 报 404 scope_not_found | scope 没创建 | 重跑第 0 步 |
