"""MiniMax / QF OpenAI-compatible ``POST .../embeddings``."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List

from research_library.analysis.llm.base import LLMError
from research_library.analysis.llm.minimax import _default_request_timeout, _load_minimax_env, _strip_key
from research_library.analysis.llm.minimax import MiniMaxChatClient
from research_library.config import load_env


class MiniMaxOpenAIEmbeddings:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
        group_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        root = MiniMaxChatClient._normalize_openai_root(base_url)
        self._base_url = root.rstrip("/")
        self._group_id = (group_id or "").strip() or None
        self.embedding_dim: int = 0

    def _embeddings_url(self) -> str:
        url = f"{self._base_url}/embeddings"
        if self._group_id and "GroupId=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode({'GroupId': self._group_id})}"
        return url

    def embed_texts(
        self,
        texts: List[str],
        *,
        model: str | None = None,
        for_query: bool = False,
    ) -> List[List[float]]:
        use_model = (model or "").strip() or self._model
        load_env()
        # MiniMax embeddings support an asymmetric ``type`` field (``query`` vs ``db``).
        # If the caller specified ``for_query`` we honor it; otherwise we fall back to
        # the env override or ``db`` (document side).
        env_type = (os.environ.get("RESEARCH_EMBEDDING_MINIMAX_TYPE") or "").strip().lower()
        if for_query:
            emb_type = "query"
        elif env_type in ("db", "query", "document"):
            emb_type = "db" if env_type == "document" else env_type
        else:
            emb_type = "db"
        payload: dict[str, Any] = {
            "model": use_model,
            "texts": texts,
        }
        if emb_type in ("db", "query"):
            payload["type"] = emb_type
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(
            self._embeddings_url(),
            data=data,
            headers=headers,
            method="POST",
        )
        to = _default_request_timeout()
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise LLMError(f"MiniMax embeddings HTTP {e.code}: {body[:2000]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"MiniMax embeddings network error: {e}") from e

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"MiniMax embeddings invalid JSON: {raw[:500]}") from e

        err = obj.get("error")
        if err:
            msg = err if isinstance(err, str) else (err.get("message") or str(err))
            raise LLMError(f"MiniMax embeddings API error: {msg}")

        base_resp = obj.get("base_resp") or {}
        code = base_resp.get("status_code")
        if code not in (None, 0):
            msg = base_resp.get("status_msg") or raw[:500]
            if code == 1008:
                msg = (
                    f"{msg} — Note: MiniMax has indicated embedding may be unavailable on some "
                    "gateways (1008 can appear even when chat billing is OK). Try base URL "
                    "https://api.minimax.io/v1 if you use .chat, set MINIMAX_GROUP_ID if required, "
                    "or use RESEARCH_EMBEDDING_PROVIDER=openai_compat for embeddings."
                )
            raise LLMError(f"MiniMax embeddings error {code}: {msg}")

        parsed = self._vectors_from_response(obj, len(texts))
        if parsed is None:
            raise LLMError(f"MiniMax embeddings: unknown response shape: {raw[:800]}")
        ordered, embedding_dim = parsed
        if len(ordered) != len(texts):
            raise LLMError(
                f"MiniMax embeddings: got {len(ordered)} vectors for {len(texts)} inputs"
            )
        self.embedding_dim = embedding_dim
        return ordered

    @staticmethod
    def _vectors_from_response(obj: dict[str, Any], n_expected: int) -> tuple[List[List[float]], int] | None:
        """MiniMax native uses ``vectors``; OpenAI-compat may use ``data[].embedding``."""
        raw_vecs = obj.get("vectors")
        if isinstance(raw_vecs, list) and raw_vecs:
            row0 = raw_vecs[0]
            if isinstance(row0, (int, float)):
                flat = [float(x) for x in raw_vecs]
                return [flat], len(flat)
            if isinstance(row0, list):
                out = [[float(x) for x in row] for row in raw_vecs if isinstance(row, list)]
                if out:
                    return out, len(out[0])
            return None

        rows = obj.get("data")
        if not isinstance(rows, list):
            return None
        by_index: dict[int, List[float]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            emb = item.get("embedding")
            if not isinstance(emb, list):
                continue
            idx = int(item.get("index", len(by_index)))
            by_index[idx] = [float(x) for x in emb]
        if not by_index:
            return None
        ordered = [by_index[i] for i in sorted(by_index)]
        if not ordered:
            return None
        return ordered, len(ordered[0])

    @classmethod
    def from_env(cls) -> MiniMaxOpenAIEmbeddings:
        load_env()
        key, _, base_raw, gid, style = _load_minimax_env()
        if style not in ("openai", "chat", "compatible", ""):
            raise LLMError(
                "MiniMax embeddings require OpenAI-compatible base URL "
                "(MINIMAX_API_STYLE=openai and MINIMAX_BASE_URL like https://api.minimax.chat/v1)"
            )
        root = MiniMaxChatClient._normalize_openai_root(base_raw)
        model = (
            _strip_key(os.environ.get("RESEARCH_EMBEDDING_MODEL") or "")
            or _strip_key(os.environ.get("MINIMAX_EMBEDDING_MODEL") or "")
            or "embo-01"
        )
        return cls(key, model=model, base_url=root, group_id=gid)
