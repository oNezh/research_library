"""Resolve chat client from env — mirrors quantitative_finance ``default_llm_client`` env.

Precedence for **provider name**:
``RESEARCH_LLM_PROVIDER`` (optional override) → ``QF_LLM_PROVIDER`` → ``minimax``.

* ``minimax`` — :class:`MiniMaxChatClient` (``QF_LLM_*`` / ``MINIMAX_*``).
* ``openai`` / ``openai_compat`` — :class:`OpenAICompatibleChatClient` (``QF_LLM_API_KEY`` /
  ``QF_LLM_BASE_URL`` / ``QF_LLM_MODEL``).
"""

from __future__ import annotations

import os

from research_library.config import load_env

from .base import ChatClient, LLMError
from .minimax import MiniMaxChatClient
from .openai_compat_chat import OpenAICompatibleChatClient


def _resolve_chat_provider(explicit: str | None) -> str:
    load_env()
    if explicit and explicit.strip():
        return explicit.strip().lower()
    r = (os.environ.get("RESEARCH_LLM_PROVIDER") or "").strip().lower()
    if r:
        return r
    qf = (os.environ.get("QF_LLM_PROVIDER") or "").strip().lower()
    if qf:
        return qf
    return "minimax"


def get_chat_client(provider: str | None = None) -> ChatClient:
    name = _resolve_chat_provider(provider)
    if name in ("minimax", "mini_max", "min_max"):
        return MiniMaxChatClient.from_env()
    if name in ("openai", "openai_compat", "openai-compatible"):
        return OpenAICompatibleChatClient.from_env()
    raise LLMError(
        f"Unknown LLM provider {name!r} — use QF_LLM_PROVIDER or RESEARCH_LLM_PROVIDER: "
        "minimax | openai (same as quantitative_finance)"
    )
