# DSL 结构说明（与 memory-tavern.ifpkg 实际内容一致）

> 本文档描述的是**交付包内的真实节点**，不是设计稿。
> 包内共 5 个节点、2 个 HTTP 节点，端点与节点标题如下。

---

## 1. 节点图

```
开始(记忆酒馆)
   ↓
用户输入            ← 单轮文本输入
   ↓
list_memory         ← POST /v1/memory/entries/list   （读记忆）
   ↓
tavern_logic        ← 代码节点：关键词匹配 + 记忆注入 → 台词
   ↓
remember_deed       ← POST /v1/memory/remember       （写记忆）
   ↓
直接回复            ← 输出台词 + 状态行
```

---

## 2. 两个 HTTP 节点的完整配置

### 2.1 `list_memory`（读）

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| URL | `http://host.docker.internal:8000/v1/memory/entries/list` |
| Headers | `Content-Type: application/json` |
| Body | `{"scope_id": "<SCOPE_ID>"}` |
| 超时/重试 | 默认，重试 3 次 |

响应形如：

```json
{"entries": [
  {"kind": "deed", "text": "deed: 在店里闹事", "state": "active",
   "citation": {"entry_id": "mem_ent_...", "entry_version_id": "mem_ver_..."}}
]}
```

`tavern_logic` 通过 `{{#list_memory.body#}}` 引用它的响应体。

### 2.2 `remember_deed`（写）

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| URL | `http://host.docker.internal:8000/v1/memory/remember` |
| Headers | `Content-Type: application/json` |
| Body | `{"scope_id": "<SCOPE_ID>", "kind": "deed", "text": "deed: {{#1793112592482.deed#}}"}` |
| 超时/重试 | 默认，重试 3 次 |

其中 `{{#1793112592482.deed#}}` 是 `tavern_logic` 节点输出的 `deed` 字段。

响应形如：

```json
{"memory": {"family": "memory", "revision": 5},
 "entry": {"citation": {"entry_id": "mem_ent_...", "entry_version_id": "mem_ver_..."}}}
```

---

## 3. `<SCOPE_ID>` 替换（导入后必做）

1. 先取得 scope id：

```powershell
cd D:\projects\qoder-paperlens\dify
python init_scope.py        # 幂等；打印 scope_id 并写入 scope.txt
```

2. 在 Dify 编排界面打开这两个节点，把 Body 里的 `<SCOPE_ID>` 换成 `scope.txt` 里的值。
3. **两处必须一致**。
4. 保存并发布。

> 也可以用环境变量方式：在 Dify 的「环境变量」里定义 `SCOPE_ID`，两个节点 Body 改成
> `{"scope_id": "{{#env.SCOPE_ID#}}"}`。这样只需维护一处（本项目交付的是占位符形式，二选一即可）。

---

## 4. `tavern_logic` 节点做什么

- 输入：`{{#用户输入#}}` 与 `{{#list_memory.body#}}`
- 解析 `entries`，统计 `kind == "deed"` 的条数 → 得到「老板记得你做过 N 种事」
- 关键词匹配当前输入，产生 `deed`（本轮行为）与台词
- 输出：`{"deed": "...", "reply": "..."}`

不含 LLM 调用，纯规则，保证任何环境可复现。

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 记忆数一直是 0 | `kind` 不是 `deed`，或两个节点 scope 不一致 | 检查两处 `<SCOPE_ID>` 是否同值；确认写入 `kind=deed` |
| `list_memory` 返回 404 | PowerContext 未启动或端口不对 | 确认 `http://127.0.0.1:8000` 可访问；容器内访问宿主机用 `host.docker.internal` |
| `remember_deed` 返回 401/403 | PowerContext 开了访问控制 | 设置 `POWERCONTEXT_SERVER_ACCESS_MODE=disabled` 或配置 token |
| 导入后看不到 `<SCOPE_ID>` | 包已更新为占位符版本 | 打开节点 Body 查看；若仍是 `scp_...`，说明导入的是旧包 |
| 新会话不记得历史 | 两个节点 scope 不同，或 `list_memory` 没接进 `tavern_logic` | 按第 2、3 节核对 |
