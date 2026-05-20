"""OpenAI-compatible chat (urllib) — same env as quantitative_finance ``default_llm_client``."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, List

from research_library.analysis.llm.base import ChatMessage, LLMError
from research_library.analysis.llm.minimax import _default_request_timeout, _strip_key
from research_library.config import load_env


def _normalize_openai_root(base_raw: str) -> str:
    r = (base_raw or "https://api.openai.com/v1").rstrip("/")
    if r.endswith("/chat/completions"):
        return r[: -len("/chat/completions")]
    return r


class OpenAICompatibleChatClient:
    """POST ``{base}/chat/completions`` — reads ``QF_LLM_*`` (same as quantitative_finance)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = _normalize_openai_root(base_url).rstrip("/")

    def _request_url(self) -> str:
        b = self._base_url
        return f"{b}/chat/completions" if not b.endswith("/chat/completions") else b

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        oa_messages = [
            {"role": m.get("role") or "user", "content": m.get("content") or ""}
            for m in messages
        ]
        limit = int(max_completion_tokens) if max_completion_tokens is not None else 4096
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": oa_messages,
            "max_tokens": limit,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(
            self._request_url(),
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
            raise LLMError(f"OpenAI-compat chat HTTP {e.code}: {body[:2000]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"OpenAI-compat network error: {e}") from e

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"OpenAI-compat invalid JSON: {raw[:500]}") from e

        err = obj.get("error")
        if err:
            msg = err if isinstance(err, str) else (err.get("message") or str(err))
            raise LLMError(f"OpenAI-compat API error: {msg}")

        choices = obj.get("choices") or []
        if not choices:
            raise LLMError("OpenAI-compat empty choices")
        message = (choices[0] or {}).get("message") or {}
        text = (message.get("content") or "").strip()
        if not text:
            raise LLMError("OpenAI-compat empty message content")
        return text

    @classmethod
    def from_env(cls) -> OpenAICompatibleChatClient:
        load_env()
        key = _strip_key(os.environ.get("QF_LLM_API_KEY") or "")
        if not key:
            raise LLMError(
                "QF_LLM_API_KEY is required when QF_LLM_PROVIDER=openai "
                "(same as quantitative_finance)"
            )
        base = (
            _strip_key(os.environ.get("QF_LLM_BASE_URL") or "")
            or "https://api.openai.com/v1"
        )
        model = _strip_key(os.environ.get("QF_LLM_MODEL") or "") or "gpt-4o-mini"
        return cls(key, model=model, base_url=base)
