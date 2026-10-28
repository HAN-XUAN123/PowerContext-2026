/**
 * 端侧向量化迷你知识库 —— 后端服务
 *
 * 职责边界：向量由浏览器端 Transformers.js 计算，后端不做任何推理；
 * 后端只负责：建集合、写 seekdb、调用 seekdb 的 hybridSearch 做混合检索。
 */

import express from "express";
import cors from "cors";
import { fileURLToPath } from "node:url";
import { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig, K } from "seekdb";

const PORT = Number(process.env.PORT || 8787);
const VECTOR_DIM = Number(process.env.VECTOR_DIM || 384);
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

/** 幂等获取集合：先取，取不到再建，建冲突（元数据已存在）再取一次。 */
async function ensureCollection() {
  if (collection) return collection;
  try {
    collection = await client.getCollection({ name: COLLECTION, embeddingFunction: null });
    if (collection) return collection;
  } catch {
    // 集合不存在，继续创建
  }
  try {
    collection = await client.createCollection({
      name: COLLECTION,
      embeddingFunction: null,
      schema: new Schema({
        vectorIndex: new VectorIndexConfig({ dimension: VECTOR_DIM }),
        fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
      }),
    });
  } catch (createError) {
    collection = await client.getCollection({ name: COLLECTION, embeddingFunction: null });
    if (!collection) throw createError;
  }
  return collection;
}

const app = express();
app.use(cors());
app.use(express.json({ limit: "64mb" }));
app.use(express.static(fileURLToPath(new URL("../web", import.meta.url))));

app.get("/api/health", async (_req, res) => {
  try {
    const col = await ensureCollection();
    res.json({ ok: true, collection: COLLECTION, dim: VECTOR_DIM, name: col?.name ?? COLLECTION, initError });
  } catch (error) {
    initError = String(error?.message || error);
    res.status(500).json({ ok: false, error: initError });
  }
});

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
    res.json({ ok: true, inserted: docs.length, timing: { seekdb_write_ms: done - commit, total_ms: done - started } });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error?.message || error) });
  }
});

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
    res.json({ ok: true, mode, count: items.length, items, timing: { seekdb_search_ms: done - started } });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error?.message || error) });
  }
});

app.listen(PORT, () => {
  console.log(`[mini-kb] http://127.0.0.1:${PORT}  (seekdb ${process.env.SEEKDB_HOST || "127.0.0.1"}:${process.env.SEEKDB_PORT || 2881})`);
});
