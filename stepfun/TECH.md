# 一页技术说明：端侧知识库（StepFun × seekdb）

## 模块结构

```
web/                     浏览器（端侧推理）
├── index.html           界面：语料导入 / 检索 / 后端徽标 / 分阶段耗时
└── app.js               Transformers.js 加载、WebGPU→WASM 降级、批量向量化、调用后端

server/                  Node 服务（零推理，只转发与落库）
├── index.js             Express + seekdb SDK：建集合、add、hybridSearch
└── package.json         依赖 seekdb@1.4.0 / express / cors

seekdb (Docker 2881)     集合 kb_docs：向量索引 + 全文索引（ik）
```

## 模型与量化

| 项 | 值 |
|---|---|
| 模型 | `Xenova/all-MiniLM-L6-v2`（HF 转换版），输出 384 维 |
| 运行时 | Transformers.js 3.3.3，`feature-extraction` pipeline，mean pooling + L2 归一化 |
| 量化方案 | int8（`model_quantized.onnx`，动态量化）为默认；WebGPU 下优先 fp16 |
| 体积 | fp32 90.4 MB → int8 22.7 MB，**压缩比 ≈ 4.0×**；fp16 45.2 MB（≈2.0×） |
| 质量 | 同查询集下对比 top-1 与 Recall@3（实测值见 README 第 6 节表格） |

量化选择理由：知识库场景对体积/首屏敏感（模型要在浏览器下载），int8 在 MiniLM 这类小模型上通常只损失 1–3 个百分点召回，换 4× 体积收益划算；fp16 依赖 WebGPU 的 fp16 能力，作为有 GPU 时的加速路径。

## 端侧运行与降级

```js
const attempts = hasWebGPU
  ? [{device:"webgpu",dtype:"fp16"}, {device:"webgpu",dtype:"q8"}, {device:"wasm",dtype:"q8"}]
  : [{device:"wasm",dtype:"q8"}];
```

- 逐个尝试，成功者记为当前后端，页面左上徽标显示 `WebGPU（fp16）` 或 `WASM（q8）`；
- 探测方式：`navigator.gpu` 存在且 pipeline 初始化成功（初始化失败也会继续降级，而不是直接报错）；
- 降级后功能完全一致，仅速度不同；徽标颜色区分（绿=WebGPU，橙=WASM）。

## seekdb 使用

```js
new Schema({
  vectorIndex:  new VectorIndexConfig({ dimension: 384 }),
  fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
})
// 写入：embeddingFunction: null（向量来自浏览器，禁用 SDK 自动向量化）
await collection.add({ ids, embeddings, documents, metadatas });
// 混合检索：一次调用，库内融合
await collection.hybridSearch({
  query: { whereDocument: { $contains: q } },
  knn:   { queryEmbeddings: [v], queryKey: K.EMBEDDING },
  nResults: k,
});
```

- 混合检索**不在客户端拼合**：前端只发一个 `/api/search`，后端只调一次 `hybridSearch`；
- 增量写入：每次导入用 `时间戳-序号` 作为 id，连续导入累加；
- 刷新策略：SDK 写入后立即可检索（实测写入 3 条后同一秒内查询即可命中），因此不需要额外的 refresh 步骤；
- 集合/会话状态：集合名、维度由环境变量固定，服务重启后复用同一集合；`hybridSearch` 抛错时自动降级纯向量检索并在 `mode` 字段标注。

## 状态与优化

| 优化点 | 做法 | 效果（实测/预期） |
|---|---|---|
| 批处理 | 文档向量化按 16 条一批送入 pipeline | 比逐条调用显著减少 JS↔WASM/GPU 往返；页面记录"文档向量化"总耗时 |
| 模型加载 | `env.useBrowserCache = true`，权重进 Cache Storage | 首次冷加载 vs 二次热加载分开记录 |
| 推理后端 | WebGPU 优先，失败降级 WASM | GPU 下矩阵运算吞吐更高；徽标明示当前后端 |
| 精度 | int8 默认，GPU 尝试 fp16 | 下载体积 4× 更小，首屏更快 |
| 检索 | 单次 `hybridSearch` 替代两次查询 + 客户端合并 | 少一个 RTT，且融合逻辑在库内；实测 3 条数据 top-3 检索 69 ms（含连接与 SQL 开销） |
| 写入 | 一次 `/api/ingest` 批量 `add` | 3 条实测 85 ms |

## 边界

- 不做云端推理：页面无任何外部推理 API 调用；模型权重下载不等于云端计算。
- 不预计算向量：`embeddings` 全部由浏览器 `app.js` 计算后提交，服务端无模型。
- 不用其他数据库：检索与存储全部走 seekdb 集合，服务端不缓存检索结果。
