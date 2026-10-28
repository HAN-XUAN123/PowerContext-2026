/**
 * 端侧向量化迷你知识库 —— 后端服务
 *
 * 职责边界（重要）：
 * - 向量由浏览器端 Transformers.js 计算，后端 **不做任何推理**；
 * - 后端只做三件事：建集合、写 seekdb、调用 seekdb 的 hybridSearch 做混合检索。
 *
 * seekdb 使用官方 JS SDK（npm 包名 `seekdb`，仓库 oceanbase/seekdb-js，版本 1.4.0）。
 */

import express from "express";
import cors from "cors";
import { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig, K } from "seekdb";

const PORT = Number(process.env.PORT || 8787);
const VECTOR_DIM = Number(process.env.VECTOR_DIM || 384); // all-MiniLM-L6-v2 输出 384 维
const COLLECTION = process.env.COLLECTION || "kb_docs";

const client = new SeekdbClient({
  host: process.env.SEEKDB_HOST || "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || "root",
  password: process.env.SEEKDB_PASSWORD || "",
  database: process.env.SEEKDB_DATABASE || "stepfun_kb",
});

let collection = null;
let initError = null;

async function ensureCollection() {
  if (collection) return collection;
  try {
    collection = await client.getCollection({ name: COLLECTION });
  } catch {
    collection = await client.createCollection({
      name: COLLECTION,
      // 向量由浏览器算好再写入，这里必须关掉 SDK 的自动向量化
      embeddingFunction: null,
      schema: new Schema({
        // 向量索引：语义检索
        vectorIndex: new VectorIndexConfig({ dimension: VECTOR_DIM }),
        // 全文索引：中文用 ik 分词，英文按词切分
        fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
      }),
    });
  }
  return collection;
}

const app = express();
app.use(cors());
app.use(express.json({ limit: "64mb" }));
app.use(express.static(new URL("../web", import.meta.url).pathname));

/** 健康检查：返回 seekdb 连接与集合状态 */
app.get("/api/health", async (_req, res) => {
  try {
    const col = await ensureCollection();
    res.json({ ok: true, collection: COLLECTION, dim: VECTOR_DIM, name: col?.name ?? COLLECTION, initError });
  } catch (error) {
    initError = String(error?.message || error);
    res.status(500).json({ ok: false, error: initError });
  }
});

/**
 * 写入文档：向量在浏览器算好，这里只落库。
 * body: { docs: [{ id, text, metadata }], embeddings: [[number]] }
 */
app.post("/api/ingest", async (req, res) => {
  const started = Date.now();
  try {
    const { docs = [], embeddings = [] } = req.body ?? {};
    if (!docs.length) return res.status(400).json({ ok: false, error: "docs is empty" });
    if (docs.length !== embeddings.length) {
      return res.status(400).json({ ok: false, error: "docs/embeddings length mismatch" });
    }
    const col = await ensureCollection();
    const commit = Date.now();
    await col.add({
      ids: docs.map((d) => String(d.id)),
      embeddings,
      documents: docs.map((d) => d.text),
      metadatas: docs.map((d) => d.metadata ?? {}),
    });
    const done = Date.now();
    res.json({
      ok: true,
      inserted: docs.length,
      timing: { seekdb_write_ms: done - commit, total_ms: done - started },
    });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error?.message || error) });
  }
});

/**
 * 混合检索：一次调用同时走全文与向量，由 seekdb 内部完成排序融合。
 * body: { query, embedding, k }
 */
app.post("/api/search", async (req, res) => {
  const started = Date.now();
  try {
    const { query = "", embedding = null, k = 5 } = req.body ?? {};
    if (!query && !embedding) return res.status(400).json({ ok: false, error: "query or embedding required" });
    const col = await ensureCollection();

    const knn = embedding ? { queryEmbeddings: [embedding], queryKey: K.EMBEDDING } : { queryTexts: [query] };
    const queryPart = query ? { whereDocument: { $contains: query } } : undefined;

    let results = null;
    let mode = "hybrid";
    try {
      results = await col.hybridSearch({ query: queryPart, knn, nResults: k });
    } catch (hybridError) {
      // 降级：纯向量检索（仍由 seekdb 执行）
      mode = "vector-only (" + String(hybridError?.message || hybridError).slice(0, 120) + ")";
      results = await col.query({ queryEmbeddings: [embedding], queryKey: K.EMBEDDING, nResults: k });
    }

    const done = Date.now();
    const items = (results?.ids?.[0] ?? []).map((id, i) => ({
      id,
      text: results?.documents?.[0]?.[i] ?? "",
      distance: results?.distances?.[0]?.[i] ?? null,
      metadata: results?.metadatas?.[0]?.[i] ?? {},
    }));
    res.json({
      ok: true,
      mode,
      count: items.length,
      items,
      timing: { seekdb_search_ms: done - started },
    });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error?.message || error) });
  }
});

app.listen(PORT, () => {
  console.log(`[mini-kb] http://127.0.0.1:${PORT}  (seekdb ${process.env.SEEKDB_HOST || "127.0.0.1"}:${process.env.SEEKDB_PORT || 2881})`);
});
