"""Generic OpenAI-compatible embeddings (e.g. swap from MiniMax without changing callers)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, List

from research_library.analysis.llm.base import LLMError
from research_library.analysis.llm.minimax import _default_request_timeout, _strip_key
from research_library.config import load_env


class OpenAICompatibleEmbeddings:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
    ) -> None:
        self._api_key = api_key
        self._model = model
        r = base_url.rstrip("/")
        if r.endswith("/embeddings"):
            self._base_url = r[: -len("/embeddings")]
        else:
            self._base_url = r
        self.embedding_dim: int = 0

    def _url(self) -> str:
        return f"{self._base_url}/embeddings"

    def embed_texts(
        self,
        texts: List[str],
        *,
        model: str | None = None,
        for_query: bool = False,
    ) -> List[List[float]]:
        use_model = (model or "").strip() or self._model
        payload: dict[str, Any] = {"model": use_model, "input": texts}
        # Some OpenAI-compatible providers (Qwen-DashScope, Voyage, etc.) accept an
        # ``input_type`` / ``encoding_format`` hint. We expose query-vs-document via
        # an env override so users can opt-in without breaking strict OpenAI servers.
        env_input_type_q = (os.environ.get("RESEARCH_EMBEDDING_QUERY_INPUT_TYPE") or "").strip()
        env_input_type_d = (os.environ.get("RESEARCH_EMBEDDING_DOC_INPUT_TYPE") or "").strip()
        if for_query and env_input_type_q:
            payload["input_type"] = env_input_type_q
        elif (not for_query) and env_input_type_d:
            payload["input_type"] = env_input_type_d
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(
            self._url(),
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
            raise LLMError(f"Embeddings HTTP {e.code}: {body[:2000]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Embeddings network error: {e}") from e

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"Embeddings invalid JSON: {raw[:500]}") from e

        err = obj.get("error")
        if err:
            msg = err if isinstance(err, str) else (err.get("message") or str(err))
            raise LLMError(f"Embeddings API error: {msg}")

        rows = obj.get("data")
        if not isinstance(rows, list):
            raise LLMError("embeddings: missing data[]")

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
            raise LLMError("embeddings: empty vectors")

        ordered = [by_index[i] for i in sorted(by_index)]
        if len(ordered) != len(texts):
            raise LLMError(
                f"embeddings: got {len(ordered)} vectors for {len(texts)} inputs"
            )
        if ordered:
            self.embedding_dim = len(ordered[0])
        return ordered

    @classmethod
    def from_env(cls) -> OpenAICompatibleEmbeddings:
        load_env()
        key = _strip_key(
            os.environ.get("RESEARCH_OPENAI_API_KEY")
            or os.environ.get("QF_LLM_API_KEY")
            or ""
        )
        if not key:
            raise LLMError(
                "Embedding (openai_compat): set RESEARCH_OPENAI_API_KEY or QF_LLM_API_KEY "
                "(same base/key as quantitative_finance when QF_LLM_PROVIDER=openai)"
            )
        base = (
            _strip_key(os.environ.get("RESEARCH_OPENAI_BASE_URL") or "")
            or _strip_key(os.environ.get("QF_LLM_BASE_URL") or "")
            or "https://api.openai.com/v1"
        )
        model = (
            _strip_key(os.environ.get("RESEARCH_EMBEDDING_MODEL") or "")
            or "text-embedding-3-small"
        )
        return cls(key, model=model, base_url=base)
