"""SeekDB 数据层：建表、写入、全文索引、向量检索、混合检索。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embed import EMBED_DIM, embed, vector_literal


@dataclass
class Chunk:
    chunk_id: int
    doc_id: str
    title: str
    body: str
    topic: str
    score: float = 0.0
    source: str = ""


class SeekdbStore:
    def __init__(self) -> None:
        self.host = os.environ.get("SEEKD_DB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("SEEKD_DB_PORT", "2881"))
        self.user = os.environ.get("SEEKD_DB_USER", "root")
        self.password = os.environ.get("SEEKD_DB_PASSWORD", "")
        self.database = os.environ.get("SEEKD_DB_NAME", "paperlens")
        self._conn = None

    # -- 连接 ---------------------------------------------------------------
    def connect(self):
        import pymysql  # 延迟导入，便于 /health 报错

        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:  # noqa: BLE001
                self._conn = None
        conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            charset="utf8mb4",
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
            cur.execute(f"USE `{self.database}`")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
                    doc_id     VARCHAR(128) NOT NULL,
                    title      VARCHAR(512) NOT NULL,
                    topic      VARCHAR(128) NOT NULL DEFAULT '',
                    body       LONGTEXT NOT NULL,
                    embedding  VECTOR(%d) NOT NULL,
                    FULLTEXT KEY ft_body (body) WITH PARSER ik
                ) DEFAULT CHARSET = utf8mb4
                """ % EMBED_DIM
            )
        self._conn = conn
        return conn

    def health(self) -> dict[str, Any]:
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return {"ok": True, "host": self.host, "port": self.port}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # -- 写入 ---------------------------------------------------------------
    def ingest_text(self, doc_id: str, title: str, topic: str, body: str) -> int:
        conn = self.connect()
        chunks = split_chunks(body)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            for piece in chunks:
                cur.execute(
                    "INSERT INTO chunks (doc_id, title, topic, body, embedding) VALUES (%s, %s, %s, %s, %s)",
                    (doc_id, title, topic, piece, vector_literal(embed(piece))),
                )
        return len(chunks)

    def ingest_directory(self, directory: Path) -> dict[str, int]:
        total_docs = total_chunks = 0
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8-sig")
            title, body, topic = parse_markdown(text, path.stem)
            total_docs += 1
            total_chunks += self.ingest_text(path.stem, title, topic, body)
        return {"documents": total_docs, "chunks": total_chunks}

    # -- 检索 ---------------------------------------------------------------
    def fulltext_search(self, query: str, limit: int, topic: str | None) -> list[Chunk]:
        conn = self.connect()
        sql = (
            "SELECT chunk_id, doc_id, title, body, topic, MATCH(body) AGAINST(%s) AS score "
            "FROM chunks WHERE MATCH(body) AGAINST(%s)"
        )
        params: list[Any] = [query, query]
        if topic:
            sql += " AND topic = %s"
            params.append(topic)
        sql += " ORDER BY score DESC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [Chunk(r[0], r[1], r[2], r[3], r[4], float(r[5] or 0.0), "fulltext") for r in rows]

    def vector_search(self, query: str, limit: int, topic: str | None) -> list[Chunk]:
        conn = self.connect()
        sql = (
            "SELECT chunk_id, doc_id, title, body, topic, "
            "cosine_distance(embedding, %s) AS distance FROM chunks"
        )
        params: list[Any] = [vector_literal(embed(query))]
        if topic:
            sql += " WHERE topic = %s"
            params.append(topic)
        sql += " ORDER BY distance ASC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        # cosine_distance 越小越相近，转成分数便于融合
        return [Chunk(r[0], r[1], r[2], r[3], r[4], 1.0 - float(r[5] or 0.0), "vector") for r in rows]

    def hybrid_search(self, query: str, top_k: int, topic: str | None = None,
                      topic_boost: float = 0.0) -> list[Chunk]:
        """全文 + 向量两路召回，用 RRF 融合；topic 命中时按记忆偏好加权。"""
        pool = max(top_k * 5, 20)
        merged: dict[int, Chunk] = {}
        for rank, chunk in enumerate(self.fulltext_search(query, pool, topic)):
            chunk.score = 1.0 / (60 + rank + 1)
            merged[chunk.chunk_id] = chunk
        for rank, chunk in enumerate(self.vector_search(query, pool, topic)):
            rrf = 1.0 / (60 + rank + 1)
            if chunk.chunk_id in merged:
                merged[chunk.chunk_id].score += rrf
                merged[chunk.chunk_id].source = "fulltext+vector"
            else:
                chunk.score = rrf
                merged[chunk.chunk_id] = chunk
        preferred = (topic or "").strip().lower()
        for chunk in merged.values():
            if preferred and chunk.topic.lower() == preferred:
                chunk.score *= 1.0 + topic_boost
        return sorted(merged.values(), key=lambda c: c.score, reverse=True)[:top_k]


def split_chunks(body: str, max_chars: int = 512) -> list[str]:
    """按段落切块，超长段落再按句号切，保证每块不超过 max_chars。"""
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > max_chars:
            cut = paragraph.rfind("。", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            pieces.append(paragraph[: cut + 1].strip())
            paragraph = paragraph[cut + 1 :].strip()
        if paragraph:
            pieces.append(paragraph)
    return pieces or [body.strip()]


def parse_markdown(text: str, fallback_title: str) -> tuple[str, str, str]:
    title = fallback_title
    topic = ""
    lines = text.splitlines()
    for line in lines:
        if line.startswith("# ") and title == fallback_title:
            title = line[2:].strip()
        if line.lower().startswith("topic:"):
            topic = line.split(":", 1)[1].strip()
    body = "\n".join(l for l in lines if not l.lower().startswith("topic:"))
    return title, body, topic
