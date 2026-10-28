# 一页技术说明：SDD 上下文管理系统

## 1. 模块结构

```
sddctx/
├── core.py     ← 三项技术的适配与上下文组装
│   ├── SeekdbIndex   规格/代码/测试的全文索引（seekdb，MySQL 协议）
│   ├── collect()     把 specs/*.md、*.py、tests/*.py 切成可检索文档
│   ├── TaskState     任务状态读写（PowerContext scoped memory）
│   └── build_context()  任务 → 上下文（规格 + 代码 + 测试 + 状态）
└── cli.py      ← 对外入口：index / build / state / check

agent/minimax_patch.py  ← 消费上下文 → 调 MiniMax → 落盘 → 跑测试 → 回写状态
demo_project/           ← 被修改的业务工程（规格 6 条 / 实现 1 个 / 测试 9 个）
```

## 2. 上下文选择方式

`build --task "<自然语言任务>"` 输出四段，选法如下：

| 段 | 选择策略 | 目的 |
|---|---|---|
| `specs` | 以任务关键词（中英混合、含 `SPEC-xxx` 形态）做全文检索；**再用全量规格兜底** | 规格是硬约束，宁可多带也不能漏 |
| `code` | 关键词 + 领域词（`discount`、`apply_discount`）检索源码文档 | 只把可能相关的文件/符号交给模型，控制 prompt 体积 |
| `tests` | 关键词 + 目标行为词（`limit`、`rejected`）检索测试文档 | 让模型知道"改完要满足哪些用例、哪些不能碰" |
| `state` | 从 PowerContext 读回 `state:` 前缀的全部条目 | 跨会话续做：已完成 / 待办 / 上次验证结果 |

关键设计：**规格不依赖检索命中率**。任务措辞再怪，全量规格也会进上下文，避免"漏约束"这一类最贵的错误；代价是 prompt 变长，靠"代码/测试只带相关片段"来平衡。

## 3. 三项技术的分工

| 技术 | 承担什么 | 为什么非它不可 |
|---|---|---|
| **seekdb** | 存 8 篇文档（6 规格 + 1 代码 + 1 测试）的全文索引，`FULLTEXT ... WITH PARSER ik` 支持中英混排检索 | 上下文必须来自真实工程资料并可增量更新：改规格 → 重新 `index` → `build` 立即反映新内容 |
| **PowerContext** | 用 scoped memory 存 `state: {completed/todo/verification/context}`，重启后读回 | 任务中断后要能续做：模型换一次会话，上下文模块仍能告诉它"上次做到哪、验证结果是什么" |
| **MiniMax** | 收下四段上下文，生成 `pricing.py` 的修改 | 规格 → 代码这一步必须由模型完成，本作品不写死补丁 |

## 4. 一次任务的时序

```
pytest(改前) 2 failed / 7 passed
      │
      ├─ sddctx index      → seekdb 8 篇文档
      ├─ sddctx build      → 规格6 + pricing.py + test_pricing.py + 状态
      ├─ agent/minimax_patch.py
      │      ├─ 发 prompt（含四段上下文）
      │      ├─ 收 MiniMax 回复 → 提取 python 代码块 → 写回 pricing.py
      │      ├─ pytest      → 9 passed (exit 0)
      │      └─ PowerContext 写入 completed / verification / context
      └─ sddctx state      → 重启后仍能读回上述记录
```

## 5. 边界与取舍

- 不实现完整 Agent 框架：只做"上下文入口 + 一次模型调用 + 测试验证"，符合题面允许复用现有 Agent/SDD 工具的定位。
- 不做向量检索：规格条数少时全文检索足够，且能解释"为什么这条规格进了上下文"（可复现性优于召回率）。
- MiniMax 单轮调用、无自动修复：回复格式不合格直接报错，避免"悄悄重试掩盖问题"。
- 扩展方向（未计入必做项）：规格 diff 检测、跨模块任务依赖图、上下文 token 预算裁剪。
