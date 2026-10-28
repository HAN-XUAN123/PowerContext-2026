# SDD 上下文管理系统（MiniMax × OceanBase）

> 赛道：MiniMax × OceanBase｜题目：规有所依——面向软件持续演进的 SDD 上下文管理系统
> 一句话：给 AI 开发助手一个**按规格驱动的上下文入口**——它只拿到"这次该遵守哪些规格、该改哪段代码、该跑哪些测试、任务已经做到哪一步"，然后由 MiniMax 完成代码修改并跑通测试。

---

## 1. 作品做什么

业务工程 `demo_project/` 是一个订单折扣计算模块，规格要求"VIP 折扣率上限 30%，超出必须抛 `ValueError`"。当前实现缺这段校验，于是：

- **目标测试失败**：`test_vip_discount_over_limit_rejected`、`test_vip_discount_slightly_over_limit_rejected`
- **回归测试通过**：另外 7 个用例（边界 0.30、0 折扣、四舍五入、普通会员、负值、金额边界）

上下文模块 `sddctx` 负责把任务需要的资料组织好交给 MiniMax；MiniMax 改完后测试全绿。

## 2. 技术分工（三项都必须实际参与）

| 技术 | 在流程中的实际作用 | 可核对点 |
|---|---|---|
| **MiniMax** | 消费上下文模块产出的 prompt，生成 `pricing.py` 的修改 | `docs/dev-records/*-prompt.md`（发给它的上下文）、`*-response.json`（原始回复）、`*-pricing.py.before/.after`（与提交代码对应的 diff） |
| **seekdb**（MySQL 协议，`127.0.0.1:2881`） | 存规格条目、源码、测试的全文索引；`sddctx build` 的检索结果直接进入上下文 | `sddctx check` / `build` 输出；数据库 `sdd_context.kb_docs` 表（6 条规格 + 1 个代码文件 + 1 个测试文件） |
| **PowerContext**（HTTP，`127.0.0.1:8000`） | 保存任务进展与验证结果（`state: {...}`），模块重启后读回并注入上下文 | `sddctx state` 输出；`docs/logs/*` 与 `build` 输出里的 `state` 段 |

> 业务工程原本没有数据库，本题不涉及旧库迁移。

## 3. 目录结构

```
minimax/
├── sddctx/                 上下文模块（CLI + 核心逻辑）
│   ├── core.py             seekdb 索引、PowerContext 状态、上下文选择
│   └── cli.py              入口：index / build / state / check
├── demo_project/           业务工程
│   ├── pricing.py          待修改的实现
│   ├── specs/discount.md   规格（6 条 SPEC）
│   └── tests/test_pricing.py  目标测试 2 个 + 回归测试 7 个
├── agent/minimax_patch.py  调用 MiniMax 完成修改并记录
├── docs/
│   ├── dev-records/        每次运行的上下文/prompt/回复/改前改后代码
│   └── logs/               pytest 日志（含命令与退出码）
├── README.md               本文件
├── verify.md               验证样例（按步骤执行）
└── ONE-PAGE.md             一页技术说明
```

## 4. 环境与依赖

| 组件 | 版本要求 | 说明 |
|---|---|---|
| Python | 3.10+ | 本项目在 3.14 上验证 |
| pymysql | ≥1.1 | 连 seekdb |
| pytest | ≥8 | 跑业务工程测试 |
| seekdb | `oceanbase/seekdb:latest`（Docker） | 监听 2881，MySQL 协议，`root` 空密码 |
| PowerContext | `powercontext[cli,server]==1.2.0` | 监听 8000，`ACCESS_MODE=disabled` |
| MiniMax | Chat Completions（`/v1/text/chatcompletion_v2`） | 需要 API Key；模型默认 `MiniMax-Text-01`，可用 `MINIMAX_MODEL` 覆盖 |

## 5. 从零到跑通

### 5.1 起依赖

```bash
# seekdb
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest

# PowerContext
uv tool install --force "powercontext[cli,server]==1.2.0"
uv tool run powercontext server run --env-file .env     # .env 见下
```

`.env` 最小内容：

```
POWERCONTEXT_HOME=/path/to/powercontext-data
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=8000
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
```

### 5.2 安装

```bash
pip install pymysql pytest
```

### 5.3 初始化数据并自检

```bash
cd minimax
export PYTHONPATH=$PWD
python -m sddctx.cli check          # seekdb 与 PowerContext 都应 ok: true
python -m sddctx.cli index          # 导入规格/代码/测试 → 期望 indexed: 8
```

### 5.4 建立任务上下文

```bash
python -m sddctx.cli build --task "VIP 折扣率超过 30% 时必须拒绝并抛 ValueError，不允许静默截断"
```

输出包含：6 条规格、`pricing.py`（符号 `apply_discount`）、`tests/test_pricing.py`（9 个用例）、`state`（已完成/待办/验证结果）。

### 5.5 用 MiniMax 完成修改

```bash
export MINIMAX_API_KEY=<你的 key>
python agent/minimax_patch.py --task "VIP 折扣率超过 30% 时必须拒绝并抛 ValueError，不允许静默截断"
```

脚本会：构造上下文 → 调 MiniMax → 写回 `demo_project/pricing.py`（保留 `.before`）→ 跑 pytest → 把结果写进 PowerContext。

> 没有 Key 时可以 `--dry-run` 验证流水线（不产生 MiniMax 记录，不能作为提交证据）。

### 5.6 验证测试

```bash
cd demo_project
python -m pytest tests/test_pricing.py -q
# 期望：9 passed
```

## 6. 调用入口（模块对外契约）

| 命令 | 输入 | 输出 |
|---|---|---|
| `python -m sddctx.cli index` | 工程目录（默认 `demo_project/`） | 入库条数、按类型统计 |
| `python -m sddctx.cli build --task "<任务>"` | 任务自然语言描述 | JSON：`specs[]` `code[]` `tests[]` `state{}` |
| `python -m sddctx.cli state [--set completed --value "..."]` | 可选写入 | 当前任务状态 |
| `python -m sddctx.cli check` | — | seekdb / PowerContext 连通性 |

`build` 的输出结构就是交给 MiniMax 的上下文，字段含义见 `docs/dev-records/*-context.json`。

## 6.1 关于仓库里的代码版本

仓库提交的 demo_project/pricing.py 是**修改后**的版本（测试全绿）。修改前的版本保存在
docs/dev-records/20261028T112652-pricing.py.before，需要复现失败基线时按 erify.md 第 1 节恢复。

## 7. 已知限制

- 上下文选择采用"关键词全文检索 + 规格全量兜底"的规则法，不做语义 embedding；规格条数多时需要人工调 `--limit`。
- `state` 以追加方式写入 PowerContext，读取时按 `state:` 前缀解析；未实现冲突合并。
- 只支持 Python 工程的规格/代码/测试切分（规格按 `## SPEC-xxx` 分节）。
- MiniMax 调用为单轮、无重试；回复格式异常时直接报错，不自动修复。

## 8. 来源说明

- 业务工程、规格文档、测试用例为本作品原创；
- seekdb、PowerContext 按其官方文档接入，未修改其源码；
- MiniMax 通过官方 Chat Completions HTTP 接口调用。
