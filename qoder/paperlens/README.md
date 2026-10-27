# 知迹 PaperLens — 文献与笔记混合检索助手

用 **seekdb**（向量 + 全文 + 结构化混合检索）存文献与笔记，用 **PowerContext**（scoped memory）记住你的研究方向、阅读进度与检索偏好，让"找资料"这件事越用越准。

> 赛题：Qoder × OceanBase / Build with Qoder
> 技术结合：seekdb 负责数据与检索，PowerContext 负责记忆与上下文复用。

---

## 1. 解决的问题

- 论文、课程笔记、实验记录散落在十几个文件夹里，靠文件名和记忆找；
- 关键词搜索找不到"意思相近但用词不同"的段落；
- 每次开新会话，助手都要重新问一遍"你在研究什么、看到哪了"。

PaperLens 把这三件事合起来：**混合检索**找到段落，**记忆**记住你的偏好和进度，下次直接接着来。

## 2. 核心功能

| 功能 | 说明 | 依赖 |
|---|---|---|
| 资料导入 | Markdown / txt 按标题切块写入 `chunks` 表 | seekdb |
| 混合检索 | 全文（IK 分词）+ 向量余弦 + 元数据过滤，融合排序 | seekdb |
| 记忆写入 | 记住研究方向、常读主题、上次读到哪 | PowerContext |
| 记忆复用 | 检索时自动带上偏好（topic boost）与进度提示 | PowerContext |
| 出处回查 | 每条结果返回文件名 + 段落序号 + 原文片段 | seekdb |

## 3. 目录结构

```
qoder-paperlens/
├── src/
│   ├── app.py            # FastAPI 服务：/ingest /search /memory /health
│   ├── store.py          # seekdb 适配层（MySQL 协议）
│   ├── memory.py         # PowerContext 适配层（HTTP API）
│   └── embed.py          # 向量化（离线确定性哈希向量，含接口位可换真模型）
├── data/sample/          # 3 篇示例文献 + 2 篇笔记
├── site/index.html       # 项目网站（Qoder Sites 发布用）
├── docs/
│   ├── verify.md         # 验证样例（输入/步骤/预期结果/技术生效证据）
│   └── qoder-dev-log.md  # Qoder 开发记录（分阶段 prompts + 截图位）
└── requirements.txt
```

## 4. 运行说明

### 4.1 环境

- Python 3.10+
- seekdb：以下二选一
  - **Docker**：`docker run -d --name seekdb -p 2881:2881 oceanbase/seekdb:latest`
  - **服务端**：按 seekdb 官方文档启动，默认监听 `2881`（MySQL 协议）
- PowerContext：按官方文档启动 HTTP 服务，默认 `http://127.0.0.1:8000`

### 4.2 安装

```bash
git clone <你的仓库地址> qoder-paperlens
cd qoder-paperlens
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4.3 配置

```bash
export SEEKD_DB_HOST=127.0.0.1
export SEEKD_DB_PORT=2881
export SEEKD_DB_USER=root
export SEEKD_DB_PASSWORD=
export SEEKD_DB_NAME=paperlens
export POWERCX_BASE_URL=http://127.0.0.1:8000
export PAPERLENS_EMBED_DIM=384
```

### 4.4 启动

```bash
python -m src.app          # 默认 http://127.0.0.1:8077
```

首次启动会自动建库、建表、建全文索引。

### 4.5 一键体验

```bash
python -m src.app --load-sample      # 导入 data/sample 下的示例资料
curl -s "http://127.0.0.1:8077/search?q=混合检索&top_k=3" | python -m json.tool
```

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 依赖检查（seekdb / PowerContext 连通性） |
| POST | `/ingest` | `{"path": "data/sample"}` 批量导入 |
| GET | `/search?q=&top_k=&topic=` | 混合检索，返回带出处的结果 |
| POST | `/memory/preference` | `{"topic": "...", "note": "..."}` 写入偏好 |
| GET | `/memory/preference` | 读回偏好（验证记忆复用） |

## 6. 技术结合点（评审可核对）

- **seekdb**：`chunks` 表同时含 `LONGTEXT body` + `FULLTEXT(body) WITH PARSER ik` + `VECTOR(384)` 列；检索语句同时用 `MATCH ... AGAINST` 与 `cosine_distance`，再用元数据（`doc_type`、`topic`）过滤，构成混合检索。
- **PowerContext**：偏好写入 `scoped memory`（scope = 用户 ID），检索时读出 `topic_boost`，对命中该主题的段落加权，并在响应里返回 `memory_used` 字段，便于评审核对"记忆确实被复用"。

## 7. 验证样例

见 `docs/verify.md`（含初始数据、三组输入、预期输出、以及 seekdb 查询日志与 PowerContext 记忆读写的核对方法）。

## 8. Qoder 开发记录

见 `docs/qoder-dev-log.md`：按"需求拆解 → 数据层 → 检索 → 记忆 → 联调 → 部署"六阶段列出可直接粘贴的 prompts 和截图位，附账号 UID 填写处。

## 9. 许可与来源

- 示例文献为自写摘要，无版权问题；
- 若替换为真实论文，请自行确认许可。
