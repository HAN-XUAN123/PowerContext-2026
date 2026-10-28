# 记忆酒馆 Memory Tavern

> Dify × OceanBase 参赛作品｜用 Dify Chatflow 做的一个会**记住你干过什么**的文字小游戏
> 技术组合：Dify（流程编排）+ PowerContext（跨会话记忆）
> 不需要任何大模型 API Key —— 游戏逻辑跑在 Dify Code 节点里

---

## 1. 作品创意

你是一家深夜酒馆的常客。每次进门，你都能说一句话或做一件事：点酒、闹事、赔钱、走人。

特别之处是**老板记得你**：

- 第一次来，他当你是生客；
- 你上次掀了桌子，这次进门他就眯起眼说「又来？上次的账还没结」；
- **哪怕你新开一个对话**，他依然记得 —— 因为你的行为被写进了 PowerContext 的长期记忆，而不是留在会话上下文里。

这就是本题想表达的"好玩"：记忆本身就是玩法。玩家的每次选择都会留下痕迹，而痕迹会在未来以台词的形式回来找你。

## 2. 玩法与体验方式

| 步骤 | 玩家动作 | 老板反应 |
|---|---|---|
| 1 | 输入「来杯最烈的酒」 | 「来了？还是老样子。」 |
| 2 | 输入「我把桌子掀了」 | 「又来？上次的账还没结。」 |
| 3 | 输入「随便说点什么」 | 「（老板显然还记得你上次闹的事。）」 |
| 4 | **新开一个对话**，输入「来杯酒」 | 「（老板记得你之前…闹事…）」，跨会话记忆生效 |

入口：Dify 应用页面的聊天窗口（Chatflow），无需额外前端。

## 3. Dify 与 PowerContext 的分工

| 组成 | 承担的工作 | 可核对点 |
|---|---|---|
| **Dify（必选）** | 全部流程编排：接收输入 → 查记忆（HTTP 节点）→ 酒馆逻辑（Code 节点）→ 写记忆（HTTP 节点）→ 组织回答（Answer 节点） | `memory-tavern.dsl.yml` 里的 5 个节点与连线；Dify 运行日志里的节点执行记录 |
| **PowerContext（记忆）** | 保存每条玩家行为（`kind=deed`），下一轮与新会话中读回并注入老板台词 | `POST /v1/memory/remember` 与 `POST /v1/memory/search` 的调用记录；返回体里的 `entry_id` / `hits` |

> 本题技术选型只要求 seekdb 与 PowerContext 二选一，本作品选择 **PowerContext**，用于跨会话记忆。

## 4. 运行环境

| 组件 | 版本 / 说明 |
|---|---|
| Dify | `langgenius/dify` 最新 `docker/docker-compose.yaml`（本文按 2025 年主线版本编写，`docker compose up -d` 一键起） |
| Docker Desktop | 4.x（Windows，需开启 WSL2 后端） |
| PowerContext | `powercontext[cli,server]==1.2.0`（`uv tool install`），HTTP 服务监听 8000 |
| Python | 3.12（PowerContext 依赖） |
| 模型服务 | **不需要**。工作流不含 LLM 节点 |

## 5. 从零到运行

### 5.1 启动 PowerContext

```bash
uv tool install --force "powercontext[cli,server]==1.2.0"
mkdir -p D:\powercontext && cd D:\powercontext
# .env 内容：
# POWERCONTEXT_HOME=D:\powercontext\data
# POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
# POWERCONTEXT_SERVER_HTTP_PORT=8000
# POWERCONTEXT_SERVER_ACCESS_MODE=disabled
# POWERCONTEXT_SERVER_MCP_ENABLED=true
uv tool run powercontext server run --env-file .env
```

启动后访问 `http://127.0.0.1:8000/health/live` 应返回 200。

### 5.2 创建游戏用的 scope

```bash
python init_scope.py
```

脚本会创建 scope（幂等，重复执行不会重复建）并把 `scope_id` 写入 `scope.txt`，同时打印出来。

### 5.3 启动 Dify（Docker）

```bash
git clone https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
docker compose up -d
```

等容器全部 healthy 后访问 `http://localhost/`，注册本地管理员账号。

### 5.4 导入 DSL

1. Dify → 应用 → **导入 DSL 文件** → 选择 `memory-tavern.dsl.yml`
2. 打开应用 → 编排界面
3. 把两个 HTTP 节点里的 `<SCOPE_ID>` 替换成 `scope.txt` 里的真实 scope_id（共 2 处：`list_memory`、`remember_deed`）
4. 保存 / 发布

> 包内交付的是**占位符** `<SCOPE_ID>`（`memory-tavern.ifpkg` 里两处），不是事先填好的 id。
> 两个节点必须替换成**同一个** scope id，否则读写落在不同命名空间，跨会话记忆不成立。
> 节点与端点对应关系：`list_memory` → `POST /v1/memory/entries/list`（读），
> `remember_deed` → `POST /v1/memory/remember`（写）。

> DSL 里两个 HTTP 节点的 URL 默认写的是 `http://host.docker.internal:8000/...`。
> 若你的 PowerContext 不在本机，改成对应地址即可。

### 5.5 打开应用开始玩

Dify → 应用 → **运行** → 在聊天框里按第 2 节的玩法输入。

## 6. 验证样例

见 [`verify.md`](./verify.md)：包含初始状态、三轮输入、预期表现、跨会话记忆验证，以及 PowerContext 侧的证据核对方法。

## 7. 已知限制

- 游戏回应是**模板规则**（关键词匹配 + 记忆注入），不接 LLM，因此台词多样性有限；这是刻意的取舍——保证评审无需任何模型 Key 就能复现。
- PowerContext 的 scope 是单一玩家维度；多玩家需要为每个玩家分配独立 scope。
- 云端 Dify 无法访问本机 `127.0.0.1:8000`，因此本说明基于**本地 Docker 版 Dify**。

## 8. 文件说明

| 文件 | 用途 |
|---|---|
| `memory-tavern.dsl.yml` | Dify Chatflow 导出的完整 DSL，导入即用 |
| `DSL-GUIDE.md` | 不导入 DSL 时，手把手重建工作流的节点配置（含 Code 节点源码） |
| `init_scope.py` | 创建 PowerContext scope 并写 `scope.txt` |
| `verify.md` | 验证样例与证据核对步骤 |
| `screenshots/` | 运行截图（节点执行记录、对话记录、PowerContext 调用记录） |

## 9. 来源说明

- 玩法与节点代码为本作品原创，未复用第三方工作流；
- PowerContext 为 OceanBase 开源项目（Apache-2.0），按其官方文档接入；
- Dify 为开源项目，未修改其源码。
