/** 把 node_modules 里的 Transformers.js 复制到 web/vendor/（仓库不提交第三方产物）。 */
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const SRC = path.join(ROOT, "server", "node_modules", "@huggingface", "transformers", "dist");
const DST = path.join(ROOT, "web", "vendor", "transformers");

if (!fs.existsSync(SRC)) {
  console.error("请先在 server/ 执行 npm install（或 pnpm install）");
  process.exit(1);
}
fs.mkdirSync(DST, { recursive: true });
for (const name of fs.readdirSync(SRC)) {
  if (name.endsWith(".js") || name.endsWith(".mjs")) {
    fs.copyFileSync(path.join(SRC, name), path.join(DST, name));
  }
}
console.log("vendor 同步完成：" + DST);
