"""PaperLens FastAPI 接口层：只负责路由与响应格式化，业务委托给 store / memory / embed。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException

from .memory import PowerContextMemory
from .store import SeekdbStore


def create_app() -> FastAPI:
    app = FastAPI(title="PaperLens", version="1.0.0")
    store = SeekdbStore()
    memory = PowerContextMemory()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"seekdb": store.health(), "powercontext": memory.health()}

    @app.post("/ingest")
    def ingest(payload: dict) -> dict[str, Any]:
        directory = Path(str(payload.get("path", "")))
        if not directory.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {directory}")
        return store.ingest_directory(directory)

    @app.get("/search")
    def search(q: str, top_k: int = 5, topic: str | None = None) -> dict[str, Any]:
        preference = memory.preference()
        effective_topic = topic or preference.get("preferred_topic")
        boost = 0.35 if preference.get("preferred_topic") else 0.0
        chunks = store.hybrid_search(q, top_k, effective_topic, boost)
        return {
            "query": q,
            "memory_used": bool(preference),
            "preference": preference,
            "results": [
                {
                    "doc_id": c.doc_id,
                    "title": c.title,
                    "topic": c.topic,
                    "score": round(c.score, 6),
                    "via": c.source,
                    "snippet": c.body[:200],
                }
                for c in chunks
            ],
        }

    @app.post("/memory/preference")
    def set_preference(payload: dict) -> dict[str, Any]:
        written = []
        topic = str(payload.get("topic", "") or "")
        progress = str(payload.get("progress", "") or "")
        if topic:
            written.append(memory.remember("preference", f"topic: {topic}"))
        if progress:
            written.append(memory.remember("progress", f"progress: {progress}"))
        return {"written": written, "scope": memory.scope}

    @app.get("/memory/preference")
    def get_preference() -> dict[str, Any]:
        return {"preference": memory.preference(), "scope": memory.scope}

    return app


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaperLens 文献与笔记混合检索助手")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--load-sample", action="store_true", help="导入 data/sample 示例资料")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.load_sample:
        sample = Path(__file__).resolve().parent.parent / "data" / "sample"
        print(json.dumps(SeekdbStore().ingest_directory(sample), ensure_ascii=False))
        return 0

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
