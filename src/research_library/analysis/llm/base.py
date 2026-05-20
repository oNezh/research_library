"""Abstract chat interface for swapping providers (MiniMax, OpenAI-compatible, etc.)."""

from __future__ import annotations

from typing import List, Protocol, TypedDict


class LLMError(RuntimeError):
    """Provider returned an error or unexpected payload."""


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatClient(Protocol):
    """Minimal contract for multi-turn text generation."""

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return assistant plain text (non-streaming)."""
