"""向量化模块：文本分词与向量生成。"""

from __future__ import annotations

import hashlib
import math
import os
import re

EMBED_DIM = int(os.environ.get("PAPERLENS_EMBED_DIM", "384"))
_TOKEN_RE = re.compile(r"[a-zA-Z]+|\d+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def embed(text: str) -> list[float]:
    """确定性哈希向量：同一文本永远得到同一向量，无需外部模型。

    真实部署时把这里换成 embedding 服务调用即可，接口保持不变。
    """
    vector = [0.0] * EMBED_DIM
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % EMBED_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
