# 验证样例 —— 记忆酒馆

> 目标：用一条可复现的路径证明两件事
> ① 应用能跑、主要玩法可体验
> ② PowerContext 的记忆真的被保存并在后续互动中复用

---

## 0. 准备（与包内节点严格对应）

包内只有**两个** HTTP 节点，它们是本作品唯一的记忆链路：

| 节点标题（Dify 编排界面可见） | 方法 | 路径 | 作用 |
|---|---|---|---|
| `list_memory` | POST | `/v1/memory/entries/list` | 会话开始/每轮读取历史记忆 |
| `remember_deed` | POST | `/v1/memory/remember` | 把本轮行为写入记忆 |

两个节点的请求体里都有 `"scope_id": "<SCOPE_ID>"`。**导入 DSL 后必须把这两处的 `<SCOPE_ID>` 替换成同一个真实 scope id**，否则读写会落在不同命名空间，跨会话记忆不成立。

获取 scope id（二选一）：

```powershell
# 方式 A：运行脚本创建（幂等，重复运行返回同一 scope）
cd D:\projects\qoder-paperlens\dify
python init_scope.py
# 输出形如：scope_id=scp_0qyxeecf7ecqvdpc2x4pzqd94r，并写入 scope.txt

# 方式 B：直接看已有的 scope.txt
Get-Content D:\projects\qoder-paperlens\dify\scope.txt
```

然后把 `scope.txt` 里的值（形如 `scp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx`）填入 DSL 两处 `<SCOPE_ID>`，保存。

> 本作品交付时 `scope.txt` 为 `scp_0qyxeecf7ecqvdpc2x4pzqd94r`，包内两处占位符替换成该值即可复现本文档的全部输出。

---

## 1. PowerContext 侧真实读写记录（可直接核对，不依赖 Dify）

这一步是"记忆链路成立"的硬证据，两个请求与包内两个节点**完全同构**。

```powershell
cd D:\projects\qoder-paperlens\dify
$s = (Get-Content scope.txt).Trim()

# 写入（等价于 remember_deed 节点）
curl.exe -s -X POST http://127.0.0.1:8000/v1/memory/remember `
  -H "Content-Type: application/json" `
  -d "{\"scope_id\":\"$s\",\"kind\":\"deed\",\"text\":\"deed: 在店里闹事\"}"

# 读取（等价于 list_memory 节点）
curl.exe -s -X POST http://127.0.0.1:8000/v1/memory/entries/list `
  -H "Content-Type: application/json" `
  -d "{\"scope_id\":\"$s\"}"
```

### 实测记录（2026-10-28，scope = `scp_0qyxeecf7ecqvdpc2x4pzqd94r`）

写入返回 `HTTP 200`：

```json
{"memory": {"family": "memory", "artifact_id": "memory", "revision": 5},
 "entry": {"citation": {"memory_ref": {"family": "memory", "artifact_id": "memory", "revision": 5},
                        "entry_id": "mem_ent_8cfda4c01485417c9207ce2df4ec8879",
                        "entry_version_id": "mem_ver_05c0d943466a4919a21be67f99591737"}}}
```

读取返回 `HTTP 200`，`count = 5`（含历史累积）：

```json
{"entries": [
  {"kind": "deed", "text": "deed: 提到赔钱",         "state": "active"},
  {"kind": "deed", "text": "deed: 点了一杯最烈的酒", "state": "active"},
  {"kind": "deed", "text": "deed: 准备离开",         "state": "active"},
  {"kind": "deed", "text": "deed: 喝了一杯酒",       "state": "active"},
  {"kind": "deed", "text": "deed: 在店里闹事",       "state": "active"}
]}
```

完整原始响应保存在同目录 `verify-evidence.json`。

> 上面"写入 → 读取"能读到同一条 `deed: 在店里闹事`，即证明 **写和读用的是同一 scope**，链路闭合。

---

## 2. Dify 侧对话验证

### 第一轮：生客

**输入**：`来杯最烈的酒`

**预期**：回答包含「来了？还是老样子。」，结尾状态行「老板记得你做过 **0** 种事」。
**证据**：`list_memory` 节点执行记录里 `entries` 为 `[]` 或不含本会话写入的 deed。

### 第二轮：闹事

**输入**：`我把桌子掀了`

**预期**：回答包含「又来？上次的账还没结。」，状态行「老板记得你做过 **1** 种事」。
**证据**：`remember_deed` 节点执行记录 `HTTP 200`，响应体含 `entry_id`（形如 `mem_ent_...`）。

### 第三轮：记忆浮现

**输入**：`随便说点什么`

**预期**：回答出现「（老板显然还记得你上次闹的事。）」，状态行「老板记得你做过 **2** 种事」。
**证据**：`list_memory` 返回的 `entries` 里能看到前面写入的 `deed: 在店里闹事`。

---

## 3. 跨会话记忆（本作品核心验证）

**操作**：在 Dify 里**新开一个对话**（不是继续原对话），第一句输入 `来杯酒`。

**预期**：回答里出现引用历史的句子（老板提起你之前闹事）。

**为什么这能证明 PowerContext 起作用**：新会话的上下文是空的，Dify 只能通过 `list_memory` 节点拿到历史；若历史不在 PowerContext 里，这句引用不可能出现。

---

## 4. 截图清单

| 编号 | 内容 |
|---|---|
| D1 | Dify 编排界面全图（5 个节点 + 连线） |
| D2 | 第一轮对话（记得 0 件事） |
| D3 | 第二轮对话（记得 1 件事，老板提账未结） |
| D4 | **新开对话后的第三轮**（引用历史，跨会话记忆生效）⭐ |
| D5 | `list_memory` / `remember_deed` 节点的执行记录（含请求 URL 与响应体） |
| D6 | 第 1 节命令的终端输出（PowerContext 里的 deed 列表） |

---

## 5. 恢复初始状态

- 换新 scope：删掉 `scope.txt` 后重跑 `init_scope.py`，再把 DSL 两处 `<SCOPE_ID>` 同步替换成新值；
- 或换一个 `kind`（如把 `deed` 改成 `deed2`），旧记忆就不会被解析。
