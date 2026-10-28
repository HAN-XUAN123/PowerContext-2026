"""SDD 上下文模块核心：seekdb 索引、PowerContext 状态、上下文选择。

三项技术分工：
- seekdb（MySQL 协议，默认 127.0.0.1:2881）：存规格条目/代码/测试的全文索引，
  按任务检索后组装上下文。
- PowerContext（HTTP，默认 127.0.0.1:8000）：存任务进展与验证结果，重启后读回。
- MiniMax：由 agent/minimax_patch.py 调用，消费本模块产出的上下文。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# seekdb 索引
# --------------------------------------------------------------------------- #


@dataclass
class Doc:
    doc_id: str
    kind: str          # spec | code | test
    path: str
    symbol: str
    title: str
    content: str


class SeekdbIndex:
    def __init__(self) -> None:
        self.host = os.environ.get("SEEKD_DB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("SEEKD_DB_PORT", "2881"))
        self.user = os.environ.get("SEEKD_DB_USER", "root")
        self.password = os.environ.get("SEEKD_DB_PASSWORD", "")
        self.database = os.environ.get("SEEKD_DB_NAME", "sdd_context")
        self._conn = None

    def connect(self):
        import pymysql

        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:  # noqa: BLE001
                self._conn = None
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, charset="utf8mb4", autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
            cur.execute(f"USE `{self.database}`")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_docs (
                    doc_id  VARCHAR(160) PRIMARY KEY,
                    kind    VARCHAR(16) NOT NULL,
                    path    VARCHAR(512) NOT NULL,
                    symbol  VARCHAR(2048) NOT NULL DEFAULT '',
                    title   VARCHAR(512) NOT NULL,
                    content LONGTEXT NOT NULL,
                    FULLTEXT KEY ft_content (title, content) WITH PARSER ik
                ) DEFAULT CHARSET = utf8mb4
                """
            )
        self._conn = conn
        return conn

    # -- 写入 ---------------------------------------------------------------
    def replace_all(self, docs: list[Doc]) -> int:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_docs")
            for doc in docs:
                cur.execute(
                    "INSERT INTO kb_docs (doc_id, kind, path, symbol, title, content) VALUES (%s,%s,%s,%s,%s,%s)",
                    (doc.doc_id, doc.kind, doc.path, doc.symbol, doc.title, doc.content),
                )
        return len(docs)

    def search(self, query: str, kind: str | None = None, limit: int = 20) -> list[Doc]:
        conn = self.connect()
        sql = ("SELECT doc_id, kind, path, symbol, title, content, MATCH(title, content) AGAINST(%s) AS score "
               "FROM kb_docs WHERE MATCH(title, content) AGAINST(%s)")
        params: list[Any] = [query, query]
        if kind:
            sql += " AND kind = %s"
            params.append(kind)
        sql += " ORDER BY score DESC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [Doc(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def by_kind(self, kind: str) -> list[Doc]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT doc_id, kind, path, symbol, title, content FROM kb_docs WHERE kind=%s", (kind,))
            rows = cur.fetchall()
        return [Doc(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


# --------------------------------------------------------------------------- #
# 工程资料切分与入库
# --------------------------------------------------------------------------- #

_SPEC_HEAD = re.compile(r"^##\s+(SPEC-[A-Z0-9-]+)\s*(.*)$", re.M)
_DEF_LINE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)
_TEST_LINE = re.compile(r"^def\s+(test_[A-Za-z0-9_]+)\s*\(", re.M)


def collect(root: Path) -> list[Doc]:
    """把 specs/、源码、tests/ 切成可检索的文档。"""
    docs: list[Doc] = []

    for spec_file in sorted((root / "specs").glob("*.md")):
        text = spec_file.read_text(encoding="utf-8")
        rel = spec_file.relative_to(root).as_posix()
        parts = _SPEC_HEAD.split(text)
        # parts = [前言, id1, title1, body1, id2, title2, body2, ...]
        for i in range(1, len(parts) - 2, 3):
            spec_id, spec_title, body = parts[i], parts[i + 1].strip(), parts[i + 2].strip()
            docs.append(Doc(
                doc_id=f"spec:{spec_id}",
                kind="spec",
                path=rel,
                symbol=spec_id,
                title=f"{spec_id} {spec_title}".strip(),
                content=body,
            ))

    for py in sorted(root.glob("*.py")):
        rel = py.as_posix().split("demo_project/")[-1]
        text = py.read_text(encoding="utf-8")
        symbols = _DEF_LINE.findall(text) or ["<module>"]
        docs.append(Doc(
            doc_id=f"code:{rel}",
            kind="code",
            path=rel,
            symbol=(",".join(symbols))[:2000],
            title=f"{rel} ({', '.join(symbols)})",
            content=text,
        ))

    for test_file in sorted((root / "tests").glob("test_*.py")):
        rel = test_file.as_posix().split("demo_project/")[-1]
        text = test_file.read_text(encoding="utf-8")
        names = _TEST_LINE.findall(text)
        docs.append(Doc(
            doc_id=f"test:{rel}",
            kind="test",
            path=rel,
            symbol=(",".join(names))[:2000],
            title=f"{rel} ({len(names)} 个用例)",
            content=text,
        ))

    return docs


# --------------------------------------------------------------------------- #
# PowerContext 任务状态
# --------------------------------------------------------------------------- #


class TaskState:
    def __init__(self) -> None:
        self.base = os.environ.get("POWERCX_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.scope = os.environ.get("POWERCX_SCOPE", "")
        self.timeout = float(os.environ.get("POWERCX_TIMEOUT", "15"))

    # -- 底层 ---------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def ensure_scope(self) -> str:
        if self.scope:
            return self.scope
        created = self._post("/v1/scopes", {
            "title": "SDD Context Task",
            "summary": "sdd context management task state",
            "idempotency_key": "sdd-ctx-v1",
        })
        self.scope = created.get("scope_id", "")
        return self.scope

    # -- 读取 ---------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读回全部以 `state:` 开头的记忆条目，还原为状态对象。"""
        scope = self.ensure_scope()
        try:
            data = self._post("/v1/memory/entries/list", {"scope_id": scope})
        except Exception:  # noqa: BLE001
            return {}
        state: dict[str, Any] = {"completed": [], "todo": [], "verification": [], "context": []}
        for entry in data.get("entries") or []:
            text = entry.get("text") or ""
            if not text.startswith("state:"):
                continue
            payload = text[len("state:"):].strip()
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for key in ("completed", "todo", "verification", "context"):
                if item.get(key):
                    state[key].append(item[key])
        return state

    # -- 写入 ---------------------------------------------------------------
    def save(self, key: str, value: Any) -> dict[str, Any]:
        scope = self.ensure_scope()
        text = "state: " + json.dumps({key: value}, ensure_ascii=False)
        try:
            return self._post("/v1/memory/remember", {"scope_id": scope, "kind": "state", "text": text})
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}


# --------------------------------------------------------------------------- #
# 上下文选择
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    task: str
    specs: list[dict[str, str]] = field(default_factory=list)
    code: list[dict[str, str]] = field(default_factory=list)
    tests: list[dict[str, str]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "specs": self.specs,
            "code": self.code,
            "tests": self.tests,
            "state": self.state,
        }


def _keywords(task: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+|SPEC-[A-Z0-9-]+|[\u4e00-\u9fff]{2,}", task)
    seen, ordered = set(), []
    for token in tokens:
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        ordered.append(token)
    return ordered


def build_context(index: SeekdbIndex, state: TaskState, task: str, limit: int = 6) -> Context:
    ctx = Context(task=task)
    keywords = _keywords(task)

    # 1) 规格：先按关键词检索，命中不足时补充全部规格（保证不漏约束）
    spec_docs: dict[str, Doc] = {}
    for kw in keywords:
        for doc in index.search(kw, kind="spec", limit=limit):
            spec_docs.setdefault(doc.doc_id, doc)
    for doc in index.by_kind("spec"):
        spec_docs.setdefault(doc.doc_id, doc)

    # 2) 代码：按关键词检索
    code_docs: dict[str, Doc] = {}
    for kw in keywords + ["discount", "apply_discount"]:
        for doc in index.search(kw, kind="code", limit=limit):
            code_docs.setdefault(doc.doc_id, doc)

    # 3) 测试：按关键词检索，并补上覆盖目标规格的用例
    test_docs: dict[str, Doc] = {}
    for kw in keywords + ["discount", "limit", "rejected"]:
        for doc in index.search(kw, kind="test", limit=limit):
            test_docs.setdefault(doc.doc_id, doc)

    ctx.specs = [
        {"id": d.symbol, "title": d.title, "path": d.path, "content": d.content}
        for d in sorted(spec_docs.values(), key=lambda d: d.symbol)
    ]
    ctx.code = [
        {"path": d.path, "symbols": d.symbol, "source": d.content}
        for d in code_docs.values()
    ]
    ctx.tests = [
        {"path": d.path, "cases": d.symbol, "source": d.content}
        for d in test_docs.values()
    ]
    ctx.state = state.load()
    return ctx
