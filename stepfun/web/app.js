/**
 * 端侧向量化 + seekdb 混合检索 —— 浏览器主逻辑
 *
 * 端侧推理：Transformers.js（@xenova/transformers），模型 Xenova/all-MiniLM-L6-v2
 * - 优先 WebGPU（device: "webgpu"，dtype: "fp16" 可行时）
 * - 不支持 WebGPU 时自动降级 WASM（dtype: "q8"，即 INT8 量化权重）
 * 后端徽标会显示当前实际使用的推理后端与权重精度。
 */

import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@xenova/transformers@3.3.3/dist/transformers.min.js";

env.allowLocalModels = false;
env.useBrowserCache = true;

const MODEL_ID = "Xenova/all-MiniLM-L6-v2";
const API = "";

const el = (id) => document.getElementById(id);
const log = (msg) => {
  const box = el("ingestLog");
  box.textContent = `[${new Date().toLocaleTimeString()}] ${msg}\n` + box.textContent;
  if (box.textContent.length > 6000) box.textContent = box.textContent.slice(0, 6000);
};

let extractor = null;
let backend = "unknown";
let dtype = "unknown";

function detectWebGPU() {
  return typeof navigator !== "undefined" && !!navigator.gpu;
}

async function loadModel() {
  const started = performance.now();
  const hasGPU = detectWebGPU();
  const attempts = hasGPU
    ? [{ device: "webgpu", dtype: "fp16" }, { device: "webgpu", dtype: "q8" }, { device: "wasm", dtype: "q8" }]
    : [{ device: "wasm", dtype: "q8" }];

  let lastError = null;
  for (const attempt of attempts) {
    try {
      extractor = await pipeline("feature-extraction", MODEL_ID, {
        device: attempt.device,
        dtype: attempt.dtype,
        quantized: attempt.dtype !== "fp16",
      });
      backend = attempt.device;
      dtype = attempt.dtype;
      const ms = Math.round(performance.now() - started);
      el("t-model").textContent = String(ms);
      el("backend").textContent = `推理后端：${backend === "webgpu" ? "WebGPU" : "WASM"}（${dtype}）`;
      el("backend").className = "badge " + (backend === "webgpu" ? "gpu" : "wasm");
      el("model").textContent = `模型：all-MiniLM-L6-v2 · ${dtype}`;
      el("model").className = "badge";
      log(`模型加载完成：${MODEL_ID}，后端 ${backend}，精度 ${dtype}，耗时 ${ms} ms`);
      return;
    } catch (error) {
      lastError = error;
      log(`后端 ${attempt.device}/${attempt.dtype} 不可用：${String(error?.message || error).slice(0, 160)}`);
    }
  }
  throw lastError;
}

async function ensureModel() {
  if (!extractor) await loadModel();
}

/** 批量向量化：batchSize 是本题的性能优化点之一 */
async function embedBatch(texts, batchSize = 16) {
  await ensureModel();
  const vectors = [];
  const started = performance.now();
  for (let i = 0; i < texts.length; i += batchSize) {
    const batch = texts.slice(i, i + batchSize);
    const output = await extractor(batch, { pooling: "mean", normalize: true });
    const rows = output.tolist();
    vectors.push(...rows);
  }
  return { vectors, ms: Math.round(performance.now() - started) };
}

function splitCorpus(raw) {
  return raw
    .split(/\n\s*\n|\n/)
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

async function ingest() {
  const raw = el("corpus").value.trim();
  if (!raw) return log("请先粘贴语料");
  const texts = splitCorpus(raw);
  const button = el("ingest");
  button.disabled = true;
  try {
    log(`开始向量化 ${texts.length} 段（批大小 16）…`);
    const { vectors, ms } = await embedBatch(texts, 16);
    el("t-embed-doc").textContent = String(ms);
    log(`向量化完成，维度 ${vectors[0]?.length ?? 0}，耗时 ${ms} ms`);

    const docs = texts.map((text, i) => ({
      id: `${Date.now()}-${i}`,
      text,
      metadata: { index: i, len: text.length },
    }));
    const started = performance.now();
    const response = await fetch(API + "/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ docs, embeddings: vectors }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "ingest failed");
    el("t-write").textContent = String(data.timing?.seekdb_write_ms ?? "—");
    log(`写入 seekdb 成功：${data.inserted} 条，写库 ${data.timing?.seekdb_write_ms} ms，总耗时 ${Math.round(performance.now() - started)} ms`);
  } catch (error) {
    log(`失败：${String(error?.message || error)}`);
  } finally {
    button.disabled = false;
  }
}

async function search() {
  const query = el("query").value.trim();
  if (!query) return log("请输入查询");
  const topk = Number(el("topk").value || 5);
  const button = el("search");
  button.disabled = true;
  const e2eStart = performance.now();
  try {
    const { vectors, ms } = await embedBatch([query], 1);
    el("t-embed-query").textContent = String(ms);

    const response = await fetch(API + "/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, embedding: vectors[0], k: topk }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "search failed");
    el("t-search").textContent = String(data.timing?.seekdb_search_ms ?? "—");
    el("t-e2e").textContent = String(Math.round(performance.now() - e2eStart));
    el("hits").innerHTML = data.items
      .map((item, i) => `
        <div class="hit">
          <div class="meta">#${i + 1} · id=${item.id} · distance=${item.distance ?? "—"} · ${data.mode}</div>
          <div class="text">${escapeHtml(item.text)}</div>
        </div>`)
      .join("") || "<p style='color:#6b7280'>没有命中结果</p>";
    log(`检索完成：${data.count} 条，模式 ${data.mode}，seekdb ${data.timing?.seekdb_search_ms} ms`);
  } catch (error) {
    log(`检索失败：${String(error?.message || error)}`);
  } finally {
    button.disabled = false;
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const SAMPLE = `seekdb 是 OceanBase 推出的嵌入式向量数据库，支持向量、全文与标量条件混合检索。
混合检索把语义相似度与关键词命中结合，避免纯向量检索漏掉精确术语。
Transformers.js 让 embedding 模型直接跑在浏览器里，用户数据不出本机。
WebGPU 能显著加速矩阵运算，不支持时回退到 WASM。
模型量化用 INT8 权重可以在体积缩小约四倍的同时保持相近的检索质量。`;

el("ingest").addEventListener("click", ingest);
el("search").addEventListener("click", search);
el("sample").addEventListener("click", () => {
  el("corpus").value = SAMPLE;
  log("已载入示例语料");
});

// 启动即探测后端与数据库
(async () => {
  const hasGPU = detectWebGPU();
  el("backend").textContent = `推理后端：${hasGPU ? "WebGPU 可用" : "WASM（无 WebGPU）"}`;
  el("backend").className = "badge " + (hasGPU ? "gpu" : "wasm");
  try {
    const health = await (await fetch(API + "/api/health")).json();
    el("db").textContent = health.ok ? `seekdb：已连接 · 集合 ${health.collection}` : `seekdb：${health.error}`;
    el("db").className = "badge " + (health.ok ? "gpu" : "wasm");
  } catch (error) {
    el("db").textContent = `seekdb：后端未启动`;
    el("db").className = "badge wasm";
  }
})();
