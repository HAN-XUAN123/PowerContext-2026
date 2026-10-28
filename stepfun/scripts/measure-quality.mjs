/**
 * 量化质量对比：同一组语料与查询，比较 int8 与 fp32 的检索结果是否一致。
 * 用法：node scripts/measure-quality.mjs
 *
 * 说明：使用 transformers.js 的 Node 后端（onnxruntime-node），
 * 与浏览器端是同一套模型文件与同一套 mean-pooling + 归一化逻辑。
 */

import fs from "node:fs";
import path from "node:path";
import { pipeline, env } from "../server/node_modules/@huggingface/transformers/dist/transformers.node.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");
env.allowLocalModels = true;
env.localModelPath = path.join(ROOT, "web", "models") + path.sep;
env.remoteHost = "https://hf-mirror.com";
env.useBrowserCache = false;

const DOCS = [
  "seekdb 是 OceanBase 推出的嵌入式向量数据库，支持向量、全文与标量条件混合检索。",
  "混合检索把语义相似度与关键词命中结合，避免纯向量检索漏掉精确术语。",
  "Transformers.js 让 embedding 模型直接跑在浏览器里，用户数据不出本机。",
  "WebGPU 能显著加速矩阵运算，不支持时回退到 WASM。",
  "模型量化用 INT8 权重可以在体积缩小约四倍的同时保持相近的检索质量。",
];

// 期望命中的文档下标（人工标注）
const QUERIES = [
  { q: "混合检索怎么做", expect: 1 },
  { q: "浏览器本地跑 embedding", expect: 2 },
  { q: "GPU 不可用会怎样", expect: 3 },
  { q: "量化后体积变小多少", expect: 4 },
  { q: "哪个数据库支持向量检索", expect: 0 },
];

function cosine(a, b) {
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i] * b[i];
  return dot; // 已归一化
}

async function evaluate(modelId, label) {
  const extractor = await pipeline("feature-extraction", modelId, { dtype: "auto" });
  const embed = async (texts) => {
    const out = await extractor(texts, { pooling: "mean", normalize: true });
    return out.tolist();
  };
  const docVecs = await embed(DOCS);
  const rows = [];
  let top1 = 0;
  let recall3 = 0;
  for (const item of QUERIES) {
    const [qv] = await embed([item.q]);
    const scores = docVecs.map((dv, i) => ({ i, s: cosine(qv, dv) }));
    scores.sort((a, b) => b.s - a.s);
    const rank = scores.findIndex((x) => x.i === item.expect) + 1;
    if (rank === 1) top1++;
    if (rank <= 3) recall3++;
    rows.push({ query: item.q, expect: item.expect, rank, top: scores[0].i });
  }
  return { label, top1: top1 / QUERIES.length, recall3: recall3 / QUERIES.length, rows };
}

const int8 = await evaluate("Xenova/all-MiniLM-L6-v2", "int8");
const fp32 = await evaluate("Xenova/all-MiniLM-L6-v2-fp32", "fp32");

for (const result of [int8, fp32]) {
  console.log(`\n[${result.label}] top1=${(result.top1 * 100).toFixed(0)}%  recall@3=${(result.recall3 * 100).toFixed(0)}%`);
  for (const row of result.rows) console.log(`  ${row.query}  期望#${row.expect} 实际排名#${row.rank}`);
}

const sameRank = int8.rows.every((row, i) => row.rank === fp32.rows[i].rank);
const summary = {
  queries: QUERIES.length,
  int8: { top1: int8.top1, recall3: int8.recall3 },
  fp32: { top1: fp32.top1, recall3: fp32.recall3 },
  identical_ranking: sameRank,
};
console.log("\n" + JSON.stringify(summary, null, 2));

const outFile = path.join(ROOT, "docs", "quality-report.json");
fs.mkdirSync(path.dirname(outFile), { recursive: true });
fs.writeFileSync(outFile, JSON.stringify({ summary, int8: int8.rows, fp32: fp32.rows }, null, 2), "utf-8");
console.log(`\n已写入 ${outFile}`);
