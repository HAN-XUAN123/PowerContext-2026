# 验证样例 —— 记忆酒馆

> 目标：用一条可复现的路径证明两件事
> ① 应用能跑、主要玩法可体验 ② PowerContext 的记忆真的被保存并在后续互动中复用

---

## 0. 初始状态

按 `README.md` 第 5 节完成：PowerContext 运行中（8000）、已执行 `init_scope.py`（`scope.txt` 有 scope_id）、Dify 已导入 DSL 且两处 `<SCOPE_ID>` 已替换。

**清空历史（可选，用于从干净状态开始）**：换一个新的 scope（重跑 `init_scope.py --new`）或直接新开一个 Dify 对话（注意：新开对话**不会**清空 PowerContext 记忆，这正是本作品要验证的点）。

---

## 1. 第一轮：生客

**输入**：`来杯最烈的酒`

**预期表现**：
- 回答包含「来了？还是老样子。」
- 结尾状态行显示「老板记得你做过 **0** 种事」

**证据**：Dify 运行日志里 `search_memory` 返回 `hits: []` 或空。

---

## 2. 第二轮：闹事

**输入**：`我把桌子掀了`

**预期表现**：
- 回答包含「又来？上次的账还没结。」
- 结尾状态行显示「老板记得你做过 **1** 种事」

**证据**：Dify 日志里 `remember_deed` 返回 200，响应体含 `entry_id`。

---

## 3. 第三轮：记忆浮现

**输入**：`随便说点什么`

**预期表现**：
- 回答出现「（老板显然还记得你上次闹的事。）」
- 状态行显示「老板记得你做过 **2** 种事」

**证据**：`search_memory` 返回的 `hits` 里能看到前面写入的 `deed: 在店里闹事`。

---

## 4. 跨会话记忆（本作品的核心验证）

**操作**：在 Dify 里**新开一个对话**（不是继续原对话），第一句输入 `来杯酒`。

**预期表现**：
- 回答里出现「（老板记得你之前…闹事…）」这类引用历史的句子
- 说明记忆来自 PowerContext，而不是当前会话上下文

**为什么这能证明 PowerContext 起作用**：新会话的上下文是空的，Dify 只能通过 `search_memory` 节点拿到历史；若历史不在 PowerContext 里，这句引用不可能出现。

---

## 5. PowerContext 侧的直接证据（可独立核对）

在终端执行（不需要 Dify）：

```powershell
cd D:\projects\qoder-paperlens\dify
.\.venv\Scripts\python.exe -c "import json,os,urllib.request as u; s=open('scope.txt').read().strip(); req=u.Request('http://127.0.0.1:8000/v1/memory/entries/list', data=json.dumps({'scope_id':s}).encode(), headers={'Content-Type':'application/json'}, method='POST'); d=json.load(u.urlopen(req)); [print(e['kind'],'|',e['text']) for e in d['entries']]"
```

**预期输出**（每玩一轮多一条）：

```
deed | deed: 点了一杯酒
deed | deed: 在店里闹事
deed | deed: 说了一句「随便说点什么」
```

这条命令直接读 PowerContext 的存储，证明"记忆确实被保存"，与 Dify 侧的对话表现互相印证。

---

## 6. 截图清单

| 编号 | 内容 |
|---|---|
| D1 | Dify 编排界面全图（5 个节点 + 连线） |
| D2 | 第一轮对话（记得 0 件事） |
| D3 | 第二轮对话（记得 1 件事，老板提账未结） |
| D4 | **新开对话后的第三轮**（引用历史，跨会话记忆生效）⭐ |
| D5 | `search_memory` / `remember_deed` 节点的执行记录（含请求 URL 与响应体） |
| D6 | 第 5 节命令的终端输出（PowerContext 里的 deed 列表） |

## 7. 恢复初始状态

- 换新 scope：删掉 `scope.txt` 后重跑 `init_scope.py`，DSL 里两处 scope 同步替换；
- 或直接换一个 `kind`（如把 `deed` 改成 `deed2`），旧记忆就不会被解析。
