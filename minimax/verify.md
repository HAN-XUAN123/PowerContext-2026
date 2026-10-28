# 验证样例 —— VIP 折扣上限校验

> 目标：按步骤复现"规格 → 上下文 → MiniMax 改代码 → 测试变绿 → 状态复用"的完整闭环。
> 每一步都给出命令、预期结果和可核对的证据文件。

---

## 0. 任务与有效规格

**任务**：VIP 折扣率超过 30% 时必须拒绝并抛 `ValueError`，不允许静默截断。

**本任务必须出现在上下文里的规格**（来自 `demo_project/specs/discount.md`）：

| 规格 | 为什么必须包含 |
|---|---|
| SPEC-DISCOUNT-002 | 定义合法区间 `[0, 0.30]`，是"超限"的判据 |
| SPEC-DISCOUNT-003 | 直接规定超限要抛 `ValueError` 且信息含 `discount rate exceeds limit` |
| SPEC-DISCOUNT-004/005/006 | 边界行为：负值拒绝、两位小数、金额必须为正 |
| SPEC-DISCOUNT-001 | 普通会员不受折扣，防止改动波及 |

**必须包含的代码来源**：`pricing.py`（符号 `apply_discount`，其中 `VIP_MAX_DISCOUNT = 0.30`）。

**必须包含的测试来源**：`tests/test_pricing.py`，其中

- 目标测试：`test_vip_discount_over_limit_rejected`、`test_vip_discount_slightly_over_limit_rejected`
- 回归测试：`test_vip_boundary_discount_allowed`、`test_vip_zero_discount`、`test_vip_rounding_half_up`、`test_normal_member_no_discount`、`test_normal_member_with_discount_rejected`、`test_negative_discount_rejected`、`test_amount_must_be_positive`

---

## 1. 初始状态（修改前）

```bash
cd minimax
export PYTHONPATH=$PWD
cd demo_project && python -m pytest tests/test_pricing.py -q
```

**预期**：

```
FAILED tests/test_pricing.py::test_vip_discount_over_limit_rejected
FAILED tests/test_pricing.py::test_vip_discount_slightly_over_limit_rejected
2 failed, 7 passed
```

> 失败原因是"功能未实现"（`DID NOT RAISE ValueError`），不是依赖缺失或环境错误。

---

## 2. 初始化数据库索引

```bash
cd minimax
python -m sddctx.cli check     # seekdb ok / powercontext ok
python -m sddctx.cli index     # {"indexed": 8, "by_kind": {"spec": 6, "code": 1, "test": 1}}
```

**数据库证据**：

```bash
python -c "import pymysql;c=pymysql.connect(host='127.0.0.1',port=2881,user='root',password='');cur=c.cursor();cur.execute('USE sdd_context');cur.execute('SELECT kind,COUNT(*) FROM kb_docs GROUP BY kind');print(cur.fetchall())"
# 预期： (('code', 1), ('spec', 6), ('test', 1))
```

---

## 3. 构造任务上下文

```bash
python -m sddctx.cli build --task "VIP 折扣率超过 30% 时必须拒绝并抛 ValueError，不允许静默截断"
```

**预期输出结构**：

```json
{
  "task": "...",
  "specs": [ {"id": "SPEC-DISCOUNT-001", ...}, ... 6 条 ],
  "code":  [ {"path": "pricing.py", "symbols": "apply_discount", "source": "..."} ],
  "tests": [ {"path": "tests/test_pricing.py", "cases": "test_vip_discount_over_limit_rejected,...", "source": "..."} ],
  "state": {"completed": [], "todo": [], "verification": [], "context": []}
}
```

**核对点**：`specs` 里必须出现 `SPEC-DISCOUNT-003`；`code[0].symbols` 为 `apply_discount`；`tests[0].cases` 含两个目标测试名。

---

## 4. 让 MiniMax 完成修改

```bash
export MINIMAX_API_KEY=<你的 key>
python agent/minimax_patch.py --task "VIP 折扣率超过 30% 时必须拒绝并抛 ValueError，不允许静默截断"
```

**预期**：

```json
{"mode": "minimax", "context": {"specs": 6, "code": 1, "tests": 1}, "test_exit_code": 0}
```

**生成的证据文件**（`docs/dev-records/`，时间戳以实际为准）：

| 文件 | 内容 |
|---|---|
| `20261028T112652-context.json` | 送进 MiniMax 的上下文（规格/代码/测试/状态） |
| `20261028T112652-prompt.md` | 实际发送的完整 prompt |
| `20261028T112652-response.json` | MiniMax 原始回复（含模型名与模式） |
| `20261028T112652-pricing.py.before` | 修改前代码 |
| `20261028T112652-pricing.py.after` | 修改后代码（与仓库中的 `pricing.py` 一致） |
| `docs/logs/20261028T112652-pytest.log` | 测试命令、输出与退出码 |

**MiniMax 的实际改动**（`pricing.py`）：

```python
    if discount_rate > VIP_MAX_DISCOUNT:
        raise ValueError("discount rate exceeds limit")
```

---

## 5. 修改后测试

```bash
cd demo_project && python -m pytest tests/test_pricing.py -q
```

**预期**：`9 passed`，退出码 0。

> 目标测试由失败转为通过，回归测试全部保持通过；测试文件本身未被修改（可用 `git diff` 或与 `*-pricing.py.before` 对照确认）。

---

## 6. 状态复用（重启模块后读取）

```bash
cd minimax
python -m sddctx.cli state
```

**预期**（含本次运行记录）：

```json
{
  "completed": ["20261028T112652 MiniMax 生成修改并通过测试"],
  "verification": ["20261028T112652 pytest exit=0"],
  "context": [{"task": "...", "specs": ["SPEC-DISCOUNT-001", ...], "code": ["pricing.py"], "tests": ["tests/test_pricing.py"]}]
}
```

再执行一次 `build`，输出里的 `state` 段会带上这些记录——说明状态从 PowerContext 读回并进入上下文。

> 说明：`docs/dev-records/` 里若有更早的 `20261028T112552` 记录，那是一次 `--dry-run` 流水线自检（未调用 MiniMax），不作为 MiniMax 参与的证据。

---

## 7. 规格变更后上下文随之更新

1. 修改 `demo_project/specs/discount.md`：把 SPEC-DISCOUNT-002 的上限从 `0.30` 改成 `0.25`，并在 SPEC-DISCOUNT-003 里写明"超限信息需包含 `discount rate exceeds limit`"（保持原文即可）。
2. 重新导入并构建：

```bash
python -m sddctx.cli index
python -m sddctx.cli build --task "VIP 折扣率超过 30% 时必须拒绝并抛 ValueError"
```

3. **核对**：`specs` 中 SPEC-DISCOUNT-002 的正文应变成"上限 0.25"——证明结果来自数据库而非固定答案。
4. 还原规格（把 0.25 改回 0.30）并重新 `index`，恢复初始状态。

---

## 8. 重置方法

```bash
# 恢复代码到修改前
cp docs/dev-records/<stamp>-pricing.py.before demo_project/pricing.py

# 清空索引（可选）
python -c "import pymysql;c=pymysql.connect(host='127.0.0.1',port=2881,user='root',password='');c.cursor().execute('DROP DATABASE IF EXISTS sdd_context')"

# 清空任务状态（可选）：换一个新 scope
unset POWERCX_SCOPE
python -m sddctx.cli check     # 会自动创建新 scope
```

---

## 9. 证据清单（提交时逐项核对）

- [x] 修改前测试失败日志（2 failed, 7 passed）
- [x] seekdb 索引统计（6 规格 / 1 代码 / 1 测试）
- [x] `build` 输出（规格、代码符号、测试用例、状态四段齐全）
- [x] MiniMax 上下文 prompt、原始回复、改前/改后代码
- [x] 修改后测试日志（9 passed，退出码 0）
- [x] PowerContext 状态读写记录
