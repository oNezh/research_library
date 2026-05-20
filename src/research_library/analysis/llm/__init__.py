"""LLM provider abstraction (used by PDF / text analysis)."""

from .base import ChatClient, ChatMessage, LLMError
from .openai_compat_chat import OpenAICompatibleChatClient
from .registry import get_chat_client

__all__ = [
    "ChatClient",
    "ChatMessage",
    "LLMError",
    "OpenAICompatibleChatClient",
    "get_chat_client",
]
