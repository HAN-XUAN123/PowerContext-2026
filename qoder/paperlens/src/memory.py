"""PowerContext 记忆层（对接真实 Server 1.2.0 接口）。

- 写入：POST /v1/memory/remember  {scope_id, kind, text}
- 读取：POST /v1/memory/search    {scope_id, query, limit} -> {"hits": [...]}
- 健康：GET  /health/live
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class PowerContextMemory:
    """scoped memory：按 scope_id 存取偏好与研究进度。"""

    def __init__(self) -> None:
        self.base = os.environ.get("POWERCX_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.scope = os.environ.get("POWERCX_SCOPE", "paperlens-demo")
        self.remember_path = os.environ.get("POWERCX_REMEMBER_PATH", "/v1/memory/remember")
        self.search_path = os.environ.get("POWERCX_RECALL_PATH", "/v1/memory/search")
        self.entries_path = os.environ.get("POWERCX_ENTRIES_PATH", "/v1/memory/entries/list")
        self.timeout = float(os.environ.get("POWERCX_TIMEOUT", "10"))

    # -- 底层 ---------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    # -- 公开接口 -----------------------------------------------------------
    def health(self) -> dict[str, Any]:
        try:
            request = urllib.request.Request(self.base + "/health/live")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                ok = response.status == 200
            return {"ok": ok, "base": self.base, "scope": self.scope}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "base": self.base}

    def remember(self, kind: str, text: str) -> dict[str, Any]:
        """写入一条记忆；失败时把错误带回给调用方而不是抛异常。"""
        try:
            return self._post(self.remember_path, {"scope_id": self.scope, "kind": kind, "text": text})
        except Exception as exc:  # noqa: BLE001
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", "replace")[:500]
                except Exception:  # noqa: BLE001
                    detail = ""
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "detail": detail}

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        try:
            data = self._post(self.search_path, {"scope_id": self.scope, "query": query, "limit": limit})
        except Exception:  # noqa: BLE001
            return []
        hits = data.get("hits") or []
        return hits if isinstance(hits, list) else []

    @staticmethod
    def _hit_text(hit: dict[str, Any]) -> str:
        for key in ("text", "content"):
            value = hit.get(key)
            if isinstance(value, str) and value:
                return value
        entry = hit.get("entry")
        if isinstance(entry, dict):
            for key in ("text", "content"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    def preference(self) -> dict[str, Any]:
        """从记忆条目里解析偏好。

        优先用 /v1/memory/entries/list（全量、不依赖 FTS 命中），失败再退回 search。
        """
        result: dict[str, Any] = {}
        entries: list[dict[str, Any]] = []
        try:
            data = self._post(self.entries_path, {"scope_id": self.scope})
            entries = data.get("entries") or []
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            entries = self.recall("topic progress preference", limit=10)

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = self._hit_text(entry)
            lowered = text.lower()
            if "retired" == str(entry.get("state", "")).lower():
                continue
            if lowered.startswith("topic:"):
                result["preferred_topic"] = text.split(":", 1)[1].strip()
            elif lowered.startswith("progress:"):
                result["progress"] = text.split(":", 1)[1].strip()
        return result
