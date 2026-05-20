"""Unit tests for MiniMax reasoning-token empty-reply retry helpers."""

from research_library.analysis.llm import minimax as mm


def test_visible_text_strips_redacted_thinking():
    msg = {
        "content": "<think>x</think>\n\nhello",
        "role": "assistant",
    }
    assert mm._minimax_visible_text_from_message(msg) == "hello"


def test_reasoning_cap_retry_detects_truncated_thinking():
    obj = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "content": "<think>\nx\n</think>",
                    "role": "assistant",
                },
            }
        ],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 64}},
    }
    assert mm._minimax_reasoning_cap_retry(obj) is True


def test_reasoning_cap_retry_false_when_visible_text():
    obj = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "<think>\n</think>\npong",
                    "role": "assistant",
                },
            }
        ],
        "usage": {},
    }
    assert mm._minimax_reasoning_cap_retry(obj) is False
