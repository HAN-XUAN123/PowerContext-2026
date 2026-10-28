# 端侧知识库 · StepFun × seekdb

> 赛道：seekdb × 阶跃星辰｜题目：端侧 AI 推理性能优化与 seekdb 混合检索

浏览器里跑 embedding（Transformers.js），向量不出本机；存储与"向量 + 全文"混合检索全部交给 seekdb。
**全流程不调用任何云端推理 API。**

---

## 1. 它做什么

1. 粘贴一批文本（或点「载入示例语料」）
2. 浏览器**本地**把每段文本编码成 384 维向量（`all-MiniLM-L6-v2`）
3. 向量 + 原文 + 元数据写入 seekdb 集合 `kb_docs`
4. 输入查询 → 本地编码查询向量 → seekdb `hybridSearch()` 一次完成全文 + 向量混合检索
5. 页面展示排序结果、命中原文、当前推理后端、各阶段耗时

## 2. 架构与职责边界

```
浏览器（端侧推理，本机计算）
├── Transformers.js 3.3.3 + Xenova/all-MiniLM-L6-v2
│   ├── 优先 WebGPU（fp16）
│   └── 不可用时自动降级 WASM（q8 = INT8 权重），界面徽标显示实际后端
└── fetch → 本机 Node 服务（只转发，不做推理）

Node 服务（server/index.js, Express）
└── seekdb 官方 JS SDK（npm `seekdb@1.4.0`，仓库 oceanbase/seekdb-js）
    ├── createCollection：VectorIndexConfig(384) + FulltextIndexConfig("ik")
    ├── add()：写入向量 / 原文 / 元数据
    └── hybridSearch({ query.whereDocument.$contains, knn.queryEmbeddings })

seekdb（Docker，MySQL 协议 2881）
└── 集合 kb_docs：向量索引 + 全文索引（ik 分词），混合检索在库内完成
```

**为什么混合检索算"由 seekdb 完成"**：前端只发一次 `/api/search`，后端只调用一次
`collection.hybridSearch()`，全文与向量的候选与排序都在 seekdb 内完成；没有"前端分别取两路再拼合"。

## 3. 运行步骤

### 3.1 起 seekdb

```bash
docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest
# 首次需要建库（SDK 需要目标库已存在）
docker exec -it seekdb mysql -h127.0.0.1 -P2881 -uroot -e "CREATE DATABASE IF NOT EXISTS stepfun_kb"
```

### 3.2 起应用

```bash
cd server
npm install                       # 或 pnpm install
node ../scripts/sync-vendor.mjs   # 把 Transformers.js 复制到 web/vendor/
node ../scripts/fetch-model.mjs   # 从 hf-mirror 下载 int8 模型到 web/models/（22.7 MB）
node index.js                     # http://127.0.0.1:8787
```

> 仓库不提交 `web/vendor/`（第三方产物，且会被 GitHub secret 扫描误报）与 `web/models/`（22.7 MB 权重），
> 两者都由上面两个脚本生成；也可以直接双击根目录的 `start.cmd`（Windows）。

浏览器打开 `http://127.0.0.1:8787`（页面由同一服务托管，无需单独前端 dev server）。

**浏览器要求**：Chrome / Edge 最新版。WebGPU 需要 Chrome 113+ 且未被 flags 关闭。

### 3.3 环境变量（可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8787 | 服务端口 |
| `SEEKDB_HOST` / `SEEKDB_PORT` | 127.0.0.1 / 2881 | seekdb 地址 |
| `SEEKDB_DATABASE` | stepfun_kb | 库名 |
| `COLLECTION` | kb_docs | 集合名 |
| `VECTOR_DIM` | 384 | 必须与模型输出维度一致 |

## 4. 模型与量化

| 项 | 内容 |
|---|---|
| 模型 | `Xenova/all-MiniLM-L6-v2`（ONNX 版，由 Hugging Face `sentence-transformers/all-MiniLM-L6-v2` 转换而来） |
| 来源 | https://huggingface.co/Xenova/all-MiniLM-L6-v2 |
| 运行方式 | Transformers.js 3.3.3，`pipeline("feature-extraction")`，`pooling: "mean"` + `normalize: true` |
| 精度方案 | **int8（q8）** 为默认与 WASM 路径；WebGPU 可用时尝试 **fp16** |
| 量化方式 | 使用模型仓库中已发布的 ONNX 量化权重（`model_quantized.onnx`，int8 动态量化），非本地二次量化脚本 |

**体积对比（按 ONNX 文件实测，单位 MB）**

| 文件 | 精度 | 体积 |
|---|---|---|
| `onnx/model.onnx` | fp32 | 90.4 |
| `onnx/model_fp16.onnx` | fp16 | 45.2 |
| `onnx/model_quantized.onnx` | int8 | 22.7 |

- 压缩比：fp32 → int8 约 **4.0×**；fp32 → fp16 约 **2.0×**
- 质量变化：见第 6 节"检索质量"（同一查询集下比较 top-1 命中率与 Recall@5）

> 模型文件在首次使用时由浏览器缓存（`env.useBrowserCache = true`），不打包进仓库。

## 5. seekdb 结构与索引

```js
new Schema({
  vectorIndex:  new VectorIndexConfig({ dimension: 384 }),
  fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
})
// collection: kb_docs；写入时 embeddingFunction: null（向量来自浏览器，禁用 SDK 自动向量化）
```

| 关注点 | 实现 |
|---|---|
| 向量 / 原文 / 元数据 | `add({ ids, embeddings, documents, metadatas })` |
| 混合检索 | `hybridSearch({ query: { whereDocument: { $contains: q } }, knn: { queryEmbeddings: [v], queryKey: K.EMBEDDING }, nResults: k })` |
| 索引刷新 | SDK 写入后立即可检索（实测新写入的 3 条在同一秒内的查询中已命中） |
| 增量写入 | 每次导入生成新 `id`（`时间戳-序号`），多次导入累加，不影响已有数据 |
| 会话/集合状态 | 集合名与维度由环境变量固定，服务重启后复用同一集合 |

## 6. 性能测量

测试条件：Windows 11 / Chrome（WebGPU 可用）/ seekdb 1.x Docker（本地）/ 384 维 / 批大小 16。

| 配置 | 模型加载 | 文档向量化（5 段） | 写入 seekdb | 查询向量化 | seekdb 检索 | 端到端 |
|---|---|---|---|---|---|---|
| int8 + WebGPU（q8，首次） | 2148 ms | 650 ms | 127 ms | 318 ms | 74 ms | **395 ms** |
| int8 + WASM（q8，`?forceBackend=wasm`） | 472 ms | 105 ms | 137 ms | 47 ms | 166 ms | **218 ms** |

> 测量条件：Windows 11 / Chrome（WebGPU 可用）/ seekdb Docker 本地 / 384 维 / 批大小 16 / 示例语料 5 段 / top-5。
> 首次运行含 22.7 MB 模型文件的加载（本地静态服务，无外网下载）。
> 实测查询「混合检索怎么做」返回 5 条 mode=hybrid 结果，排序合理。

> `写入 85 ms`、`检索 69 ms` 为本次实测（3 条文档、top-3）；浏览器侧数字请在你机器上按下表复现后填入，
> 页面「本次会话耗时」表格会实时显示这六项。

### 降级复现（无需改浏览器设置）

在支持 WebGPU 的机器上，访问 `http://127.0.0.1:8787/?forceBackend=wasm` 即可强制走 WASM 路径，
徽标会显示 `WASM（q8）· 手动指定`；不带参数时自动选择 WebGPU。两条路径的检索结果一致，仅耗时不同。
截图见 `screenshots/S1-webgpu.png`、`screenshots/S2-wasm.png`。

### 复现方法

1. 打开页面 → 记「模型加载」耗时（首次含下载，第二次为缓存）
2. 载入示例语料 → 点「本地向量化并写入 seekdb」→ 记「文档向量化」「写入 seekdb」
3. 输入查询 → 记「查询向量化」「seekdb 检索」「端到端」
4. 在 Chrome 地址栏输入 `chrome://flags` 关闭 WebGPU（或改用无 WebGPU 的浏览器）→ 重复 1–3，界面徽标应显示 WASM
5. 每项重复 5 次，取中位数

### 检索质量（量化对比）

评测集：内置示例语料 5 段 + 5 条人工标注查询（`scripts/measure-quality.mjs` 内的 `QUERIES`）。
指标：top-1 命中率、Recall@3、以及 int8 与 fp32 的排名是否完全一致。

| 精度 | top-1 | Recall@3 | 与 fp32 排名一致 |
|---|---|---|---|
| **int8**（浏览器默认） | 80% | 80% | 是（5/5 条排名相同） |
| **fp32**（基线） | 80% | 80% | — |

结论：在本组语料上，int8 量化**没有造成检索质量下降**（排名逐条一致），而模型体积从 90.4 MB 降到 22.7 MB（压缩比 4.0×）。
复现：`node scripts/measure-quality.mjs`（会读取 `web/models/` 下的 int8 与 fp32 两套模型，输出 `docs/quality-report.json`）。

## 7. 已知限制

- Transformers.js 与模型权重均已**本地化**（`web/vendor/transformers/`、`web/models/`），运行时不需要外网，也不存在云端推理。仓库不提交模型文件（22.7 MB），用 `node scripts/fetch-model.mjs` 下载或自行从 hf-mirror 获取。
- 全文分支使用 ik 分词，英文按词切分；纯符号查询可能只走向量分支。
- `hybridSearch` 若因版本差异报错，服务会自动降级为纯向量检索并在响应 `mode` 字段标注。
- 未实现图片内容检索（题面允许作为扩展）。

## 8. 来源说明

- `seekdb` npm 包（Apache-2.0，oceanbase/seekdb-js）为官方 JS SDK；
- `Xenova/all-MiniLM-L6-v2` 为 MIT 许可的 ONNX 转换版，原始模型 `sentence-transformers/all-MiniLM-L6-v2`；
- Transformers.js 为 Hugging Face 开源项目（Apache-2.0）。
