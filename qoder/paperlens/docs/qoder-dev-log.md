# Qoder 开发记录（PaperLens）

> 用途：证明"项目的主要开发工作在 Qoder 中完成"。
> 填法：按下面 6 个阶段，把每条 prompt 粘贴进 Qoder，截图（含 Qoder 界面与输出），放在对应位置。
> 账号 UID：`<在此填写你的 Qoder 账号 UID>`

---

## 阶段 0：环境与项目初始化

**Prompt 1**
```
在 D:\projects 下创建一个 Python 项目 qoder-paperlens，使用 FastAPI，
目录结构：src/{app.py,store.py,memory.py,embed.py}、data/sample/、site/、docs/、requirements.txt。
先只生成骨架和 README 大纲，不要写业务逻辑。
```
**截图位 P0-1**：Qoder 生成目录结构的画面

---

## 阶段 1：需求拆解

**Prompt 2**
```
需求：做一个文献与笔记检索助手，解决三件事——
1) 论文/笔记散落在文件夹里，按文件名找不到内容；
2) 关键词搜不到"意思相近但用词不同"的段落；
3) 每次新会话都要重新说明研究方向和进度。
请把它拆成可实现的模块，并说明每个模块依赖 seekdb 还是 PowerContext。
```
**截图位 P1-1**：Qoder 输出的模块拆解

---

## 阶段 2：数据层（seekdb）

**Prompt 3**
```
用 seekdb（MySQL 协议）实现 store.py：
1) 建库 paperlens 和表 chunks(chunk_id, doc_id, title, topic, body LONGTEXT,
   embedding VECTOR(384), FULLTEXT KEY ft_body(body) WITH PARSER ik)；
2) 按段落切块写入，超长段落按句号再切；
3) 实现 fulltext_search（MATCH...AGAINST）和 vector_search（cosine_distance）。
用 pymysql，连接参数从环境变量读取。
```
**截图位 P2-1**：Qoder 生成建表与检索代码

---

## 阶段 3：混合检索

**Prompt 4**
```
在 store.py 里加 hybrid_search：全文和向量各召回 pool=max(top_k*5,20) 条，
用 RRF（1/(60+rank)）融合，topic 命中时乘以 1+boost。
要求返回结果带 via 字段标明来自 fulltext / vector / fulltext+vector。
```
**截图位 P3-1**：RRF 融合代码

---

## 阶段 4：记忆（PowerContext）

**Prompt 5**
```
用 PowerContext 的 scoped memory 实现 memory.py：
1) remember(kind, text) 写入偏好与进度；
2) recall(query, limit) 读回记忆；
3) preference() 从记忆里解析 topic: / progress: 两种前缀。
HTTP 路径要能用环境变量覆盖，服务不可用时返回空列表而不是抛异常。
```
**截图位 P4-1**：记忆读写代码

---

## 阶段 5：联调与调试

**Prompt 6**
```
/search 接口里先读 PowerContext 偏好，再调用 hybrid_search，
返回 memory_used 和 preference 字段，方便核对记忆是否被复用。
启动时如果 seekdb 连不上要给出清晰错误。
```
**截图位 P5-1**：Qoder 修复启动报错 / 联调成功的终端输出

**Prompt 7（调试用，示例）**
```
启动时报 pymysql.err.OperationalError 2003，帮我看是 seekdb 没起来还是端口不对。
```
**截图位 P5-2**：Qoder 定位连接问题

---

## 阶段 6：发布

**Prompt 8**
```
基于 README 生成一个单页项目网站 site/index.html：
包含项目简介、解决的问题、核心功能、技术结合（seekdb/PowerContext 各自作用）、
运行步骤、验证样例、代码仓库链接、演示截图占位。风格简洁，中文。
```
**截图位 P6-1**：Qoder 生成网站 + 在 Qoder Sites 点击发布的画面

---

## 核对清单

- [ ] 6 个阶段各有截图，能看出是 Qoder 界面
- [ ] 截图里的代码与仓库里的 `src/app.py` 一致
- [ ] 站点在无痕窗口可访问
- [ ] 站点里写明 Qoder 账号 UID
