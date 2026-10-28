/**
 * 下载端侧模型文件到 web/models/（仓库不提交 22.7 MB 的模型权重）。
 * 用法：node scripts/fetch-model.mjs
 */
import fs from "node:fs";
import path from "node:path";

const BASE = process.env.MODEL_HOST || "https://hf-mirror.com";
const MODEL = "Xenova/all-MiniLM-L6-v2";
const FILES = ["config.json", "tokenizer.json", "tokenizer_config.json", "onnx/model_quantized.onnx"];
const ROOT = path.resolve(import.meta.dirname, "..", "web", "models", MODEL);

for (const file of FILES) {
  const out = path.join(ROOT, file);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  const url = `${BASE}/${MODEL}/resolve/main/${file}`;
  process.stdout.write(`downloading ${file} ... `);
  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok) {
    console.log(`FAILED (${response.status})`);
    continue;
  }
  const buffer = Buffer.from(await response.arrayBuffer());
  fs.writeFileSync(out, buffer);
  console.log(`${(buffer.length / 1024 / 1024).toFixed(2)} MB`);
}
console.log(`\n模型已就绪：${ROOT}`);
