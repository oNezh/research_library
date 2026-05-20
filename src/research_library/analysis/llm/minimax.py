"""MiniMax: OpenAI-compatible ``/v1/chat/completions`` (default) or native ``chatcompletion_v2``.

**quantitative_finance parity:** QF's :class:`MiniMaxClient` only uses
``https://api.minimax.chat/v1`` + ``POST .../chat/completions`` (OpenAI-style
body via ``OpenAICompatibleClient``). It reads ``QF_LLM_API_KEY`` /
``QF_LLM_BASE_URL`` / ``QF_LLM_MODEL`` with fallback to ``MINIMAX_*`` — this
module follows the same precedence for those variables.

Native ``https://api.minimax.io/.../chatcompletion_v2`` remains opt-in via
``MINIMAX_API_STYLE=native`` or a ``chatcompletion_v2`` URL in
``MINIMAX_BASE_URL`` / legacy ``MINIMAX_API_BASE`` (QF does not use those
endpoints).

Env summary: ``QF_LLM_API_KEY`` → ``MINIMAX_API_KEY``;
``QF_LLM_BASE_URL`` → ``MINIMAX_BASE_URL`` → ``MINIMAX_API_BASE``;
``QF_LLM_MODEL`` → ``MINIMAX_MODEL``; optional ``MINIMAX_GROUP_ID``.
Each successful completion sets ``client.last_usage`` and, if
``RESEARCH_LLM_LOG_USAGE`` / ``MINIMAX_LOG_USAGE`` is ``1``/``true``/``yes``/``stderr``,
prints a JSON line to stderr.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple

from research_library.config import load_env

from .base import LLMError, ChatMessage

_THINK_RE = re.compile(
    r"<think>.*?</think>",
    flags=re.DOTALL | re.IGNORECASE,
)


def _strip_key(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def _strip_thinking_markup(text: str) -> str:
    t = _THINK_RE.sub("", text).strip()
    if t.lower().startswith("<think>"):
        i = t.find("}")
        if i == -1:
            i = t.find("\n\n")
        if i != -1:
            t = t[i + 1 :].strip()
    return t


def _default_max_completion_tokens() -> int:
    """When ``max_completion_tokens`` is omitted: high floor for reasoning-style models."""
    load_env()
    raw = (os.environ.get("RESEARCH_LLM_MAX_COMPLETION_TOKENS") or "").strip()
    try:
        return max(256, int(raw)) if raw else 32768
    except ValueError:
        return 32768


def _minimax_visible_text_from_message(message: Dict[str, Any]) -> str:
    text = (message.get("content") or "").strip()
    text = _strip_thinking_markup(text)
    if text:
        return text
    details = message.get("reasoning_details")
    if isinstance(details, list):
        chunks = []
        for d in details:
            if isinstance(d, dict) and d.get("text"):
                chunks.append(str(d["text"]))
        text = _strip_thinking_markup("\n".join(chunks)).strip()
    return text or ""


def _minimax_reasoning_cap_retry(obj: Dict[str, Any]) -> bool:
    """True when visible reply is empty but the model likely hit max_tokens in reasoning markup."""
    choices = obj.get("choices") or []
    if not choices:
        return False
    message = (choices[0] or {}).get("message") or {}
    raw = (message.get("content") or "").strip()
    if not raw or _minimax_visible_text_from_message(message):
        return False
    if "<think>" not in raw.lower():
        return False
    ch0 = choices[0] or {}
    if ch0.get("finish_reason") == "length":
        return True
    u = obj.get("usage")
    if isinstance(u, dict):
        det = u.get("completion_tokens_details")
        if isinstance(det, dict) and int(det.get("reasoning_tokens") or 0) > 0:
            return True
    return False


def _minimax_bumped_completion_cap(limit: int) -> int:
    """One retry: double cap (floor 512, ceiling max(64k, default completion env))."""
    base = _default_max_completion_tokens()
    return min(max(limit * 2, 512), max(65536, base))


def _usage_from_response_body(obj: dict[str, Any]) -> dict[str, Any]:
    u = obj.get("usage")
    if not isinstance(u, dict):
        return {}
    out: dict[str, Any] = {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "total_tokens": int(u.get("total_tokens") or 0),
    }
    details = u.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = int(details["reasoning_tokens"])
    m = obj.get("model")
    if m:
        out["response_model"] = str(m)
    return out


def _apply_usage_to_client(
    client: Any,
    obj: dict[str, Any],
) -> None:
    usage = _usage_from_response_body(obj)
    client.last_usage = usage if usage else None
    if not usage:
        return
    load_env()
    flag = (
        os.environ.get("RESEARCH_LLM_LOG_USAGE") or os.environ.get("MINIMAX_LOG_USAGE") or ""
    ).strip().lower()
    if flag in ("1", "true", "yes", "stderr"):
        print(
            "[minimax usage]",
            json.dumps(usage, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )


def _default_request_timeout() -> float:
    load_env()
    raw = (os.environ.get("RESEARCH_LLM_TIMEOUT") or os.environ.get("MINIMAX_HTTP_TIMEOUT") or "").strip()
    try:
        return max(30.0, float(raw)) if raw else 600.0
    except ValueError:
        return 600.0


def _http_retry_attempts() -> int:
    load_env()
    raw = (os.environ.get("RESEARCH_LLM_HTTP_RETRIES") or "").strip()
    try:
        return max(1, min(8, int(raw))) if raw else 3
    except ValueError:
        return 3


def _http_retry_base_delay_sec() -> float:
    load_env()
    raw = (os.environ.get("RESEARCH_LLM_HTTP_RETRY_DELAY_SEC") or "").strip()
    try:
        return max(0.5, float(raw)) if raw else 2.0
    except ValueError:
        return 2.0


def _is_retryable_minimax_network_err(e: BaseException) -> bool:
    if isinstance(e, (TimeoutError, socket.timeout, ConnectionError, BrokenPipeError)):
        return True
    if isinstance(e, http.client.IncompleteRead):
        return True
    if isinstance(e, urllib.error.URLError):
        r = getattr(e, "reason", None)
        if isinstance(r, (TimeoutError, socket.timeout, ConnectionError, BrokenPipeError)):
            return True
    return False


def _urlopen_read_body(req: urllib.request.Request, *, timeout: float) -> bytes:
    """``urlopen`` + read full body, with retries for transient disconnects (e.g. ``RemoteDisconnected``)."""
    attempts = _http_retry_attempts()
    base_delay = _http_retry_base_delay_sec()
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            if not _is_retryable_minimax_network_err(e):
                raise
            last = e
            if attempt + 1 >= attempts:
                break
            time.sleep(base_delay * (2**attempt))
    assert last is not None
    raise LLMError(
        f"MiniMax network error after {attempts} attempts: {last!r}"
    ) from last


class MiniMaxOpenAIClient:
    """POST ``{base}/chat/completions`` — OpenAI-style body (same as quantitative_finance)."""

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
        self._base_url = base_url.rstrip("/")
        self._group_id = (group_id or "").strip() or None
        self.last_usage: dict[str, Any] | None = None

    def _request_url(self) -> str:
        base = self._base_url
        if base.endswith("/chat/completions"):
            url = base
        else:
            url = f"{base}/chat/completions"
        if self._group_id and "GroupId=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode({'GroupId': self._group_id})}"
        return url

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        oa_messages = [{"role": m.get("role") or "user", "content": m.get("content") or ""} for m in messages]
        limit = (
            max_completion_tokens
            if max_completion_tokens is not None
            else _default_max_completion_tokens()
        )
        to = _default_request_timeout()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        for attempt in range(2):
            payload: dict = {"model": self._model, "messages": oa_messages, "max_tokens": limit}
            if temperature is not None:
                payload["temperature"] = temperature
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self._request_url(),
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                raw = _urlopen_read_body(req, timeout=to).decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                raise LLMError(f"MiniMax HTTP {e.code}: {body[:2000]}") from e
            except LLMError:
                raise
            except urllib.error.URLError as e:
                raise LLMError(f"MiniMax network error: {e}") from e

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise LLMError(f"MiniMax invalid JSON: {raw[:500]}") from e

            err = obj.get("error")
            if err:
                msg = err if isinstance(err, str) else (err.get("message") or str(err))
                raise LLMError(f"MiniMax API error: {msg}")

            base_resp = obj.get("base_resp") or {}
            code = base_resp.get("status_code")
            if code not in (None, 0):
                msg = base_resp.get("status_msg") or raw[:500]
                raise LLMError(f"MiniMax API error {code}: {msg}")

            choices = obj.get("choices") or []
            if not choices:
                raise LLMError("MiniMax empty choices")
            message = (choices[0] or {}).get("message") or {}
            text = _minimax_visible_text_from_message(message)
            if text:
                _apply_usage_to_client(self, obj)
                return text
            if attempt == 0 and _minimax_reasoning_cap_retry(obj):
                limit = _minimax_bumped_completion_cap(limit)
                continue
            raise LLMError("MiniMax empty message content")
        raise LLMError("MiniMax empty message content")


class MiniMaxNativeClient:
    """POST ``.../text/chatcompletion_v2`` — MiniMax native JSON schema."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        endpoint_url: str,
        group_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._endpoint_url = endpoint_url.rstrip("/")
        self._group_id = (group_id or "").strip() or None
        self.last_usage: dict[str, Any] | None = None

    def request_url(self) -> str:
        u = self._endpoint_url
        if not self._group_id:
            return u
        q = urllib.parse.urlencode({"GroupId": self._group_id})
        sep = "&" if "?" in u else "?"
        return f"{u}{sep}{q}"

    @staticmethod
    def _to_native_messages(messages: List[ChatMessage]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            role = m.get("role") or "user"
            content = m.get("content") or ""
            item: dict = {"role": role, "content": content}
            if role == "user":
                item["name"] = "User"
            elif role == "system":
                item["name"] = "system"
            elif role == "assistant":
                item["name"] = "assistant"
            out.append(item)
        return out

    def chat(
        self,
        messages: List[ChatMessage],
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        limit = (
            max_completion_tokens
            if max_completion_tokens is not None
            else _default_max_completion_tokens()
        )
        to = _default_request_timeout()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if self._group_id:
            headers["GroupId"] = self._group_id

        for attempt in range(2):
            payload: dict = {
                "model": self._model,
                "messages": self._to_native_messages(messages),
                "stream": False,
                "max_completion_tokens": limit,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.request_url(),
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                raw = _urlopen_read_body(req, timeout=to).decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                raise LLMError(f"MiniMax HTTP {e.code}: {body[:2000]}") from e
            except LLMError:
                raise
            except urllib.error.URLError as e:
                raise LLMError(f"MiniMax network error: {e}") from e

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                raise LLMError(f"MiniMax invalid JSON: {raw[:500]}") from e

            base_resp = obj.get("base_resp") or {}
            code = base_resp.get("status_code")
            if code not in (None, 0):
                msg = base_resp.get("status_msg") or raw[:500]
                hint = ""
                if code == 2049:
                    hint = (
                        " （可改用 OpenAI 兼容：删掉 MINIMAX_API_BASE 中的 chatcompletion_v2，"
                        "或设置 MINIMAX_API_STYLE=openai 与 MINIMAX_BASE_URL=https://api.minimax.chat/v1 "
                        "与 quantitative_finance 一致；或补全 MINIMAX_GROUP_ID。）"
                    )
                raise LLMError(f"MiniMax API error {code}: {msg}{hint}")

            choices = obj.get("choices") or []
            if not choices:
                raise LLMError("MiniMax empty choices")
            message = (choices[0] or {}).get("message") or {}
            text = _minimax_visible_text_from_message(message)
            if text:
                _apply_usage_to_client(self, obj)
                return text
            if attempt == 0 and _minimax_reasoning_cap_retry(obj):
                limit = _minimax_bumped_completion_cap(limit)
                continue
            raise LLMError("MiniMax empty message content")
        raise LLMError("MiniMax empty message content")


def _load_minimax_env() -> Tuple[str, str, str, str | None, str]:
    """Returns key, model, base_raw_or_empty, group_id, style.

    Variable precedence matches ``quantitative_finance.analysis.llm.default_llm_client``
    for minimax: ``QF_LLM_*`` overrides, then ``MINIMAX_*`` (plus legacy
    ``MINIMAX_API_BASE`` as last resort for base URL only).
    """
    load_env()
    key = _strip_key(os.environ.get("QF_LLM_API_KEY") or "") or _strip_key(
        os.environ.get("MINIMAX_API_KEY") or ""
    )
    if not key:
        raise LLMError(
            "MiniMax API key not set — use MINIMAX_API_KEY or QF_LLM_API_KEY "
            "(same as quantitative_finance)"
        )
    model = (
        _strip_key(os.environ.get("QF_LLM_MODEL") or "")
        or _strip_key(os.environ.get("MINIMAX_MODEL") or "")
        or "MiniMax-M2.7"
    )
    gid_raw = (os.environ.get("MINIMAX_GROUP_ID") or "").strip()
    gid = _strip_key(gid_raw) if gid_raw else None
    style = (os.environ.get("MINIMAX_API_STYLE") or "openai").strip().lower()
    base_candidate = (
        (os.environ.get("QF_LLM_BASE_URL") or "").strip()
        or (os.environ.get("MINIMAX_BASE_URL") or "").strip()
        or (os.environ.get("MINIMAX_API_BASE") or "").strip()
    )
    base_raw = _strip_key(base_candidate) if base_candidate else ""
    return key, model, base_raw, gid, style


def _use_native(style: str, base_raw: str) -> bool:
    if style in ("native", "chatcompletion_v2", "v2"):
        return True
    if style in ("openai", "chat", "compatible", ""):
        if "chatcompletion_v2" in base_raw.lower():
            return True
        return False
    raise LLMError(
        f"Unknown MINIMAX_API_STYLE={style!r}; use openai | native"
    )


class MiniMaxChatClient:
    """Factory façade — default matches quantitative_finance (OpenAI-compatible endpoint)."""

    @staticmethod
    def _normalize_openai_root(base_raw: str) -> str:
        root = base_raw or "https://api.minimax.chat/v1"
        r = root.rstrip("/")
        if r.endswith("/chat/completions"):
            return r[: -len("/chat/completions")]
        return r

    @classmethod
    def from_env(cls) -> MiniMaxOpenAIClient | MiniMaxNativeClient:
        key, model, base_raw, gid, style = _load_minimax_env()
        native = _use_native(style, base_raw)
        if native:
            endpoint = base_raw or "https://api.minimax.io/v1/text/chatcompletion_v2"
            if "chatcompletion_v2" not in endpoint.lower():
                endpoint = "https://api.minimax.io/v1/text/chatcompletion_v2"
            return MiniMaxNativeClient(
                key, model=model, endpoint_url=endpoint, group_id=gid
            )
        root = cls._normalize_openai_root(base_raw)
        return MiniMaxOpenAIClient(key, model=model, base_url=root, group_id=gid)
