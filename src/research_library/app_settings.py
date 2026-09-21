"""Runtime app settings persisted in the data directory."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from research_library.config import get_data_dir

_SECRET_KEYS = frozenset(
    {
        "api_token",
        "api_key",
    }
)

_LLM_PROVIDERS = frozenset({"minimax", "openai_compat", "openai"})
_EMBEDDING_PROVIDERS = frozenset(
    {"minimax", "openai_compat", "local_sentence_transformer"}
)


def default_settings() -> Dict[str, Any]:
    return {
        "ads": {"api_token": ""},
        "llm": {
            "provider": "openai_compat",
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
        "embedding": {
            "provider": "local_sentence_transformer",
            "api_key": "",
            "base_url": "",
            "model": "",
            "local_model": "Qwen/Qwen3-Embedding-4B",
            "device": "cpu",
            "hf_home": "",
            "hf_offline": "1",
        },
        "zotero": {
            "library_id": "",
            "api_key": "",
            "library_type": "user",
        },
    }


def settings_path() -> Path:
    return get_data_dir() / "app_settings.json"


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def settings_file_exists() -> bool:
    return settings_path().is_file()


def load_saved_settings() -> Dict[str, Any]:
    """Return only what the user saved in app_settings.json (empty if missing)."""
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_app_settings() -> Dict[str, Any]:
    path = settings_path()
    base = default_settings()
    if not path.is_file():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(raw, dict):
        return base
    return _deep_merge(base, raw)


def save_app_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = load_app_settings()
    merged = _deep_merge(current, patch)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return merged


def _mask_value(key: str, value: str) -> str:
    if key not in _SECRET_KEYS or not value:
        return value
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}…{value[-4:]}"


def mask_secrets(settings: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(settings)
    for section in ("ads", "llm", "embedding", "zotero"):
        block = out.get(section)
        if not isinstance(block, dict):
            continue
        for k, v in list(block.items()):
            if isinstance(v, str):
                block[k] = _mask_value(k, v)
    return out


def _set_env_if_unset(name: str, value: str) -> None:
    if not (value or "").strip():
        return
    if not (os.environ.get(name) or "").strip():
        os.environ[name] = value.strip()


def apply_to_environ(settings: Dict[str, Any] | None = None) -> None:
    """Map app_settings.json into os.environ when env vars are not already set.

    If the user has never saved settings (no app_settings.json), only apply values
    present in an explicit *settings* argument (e.g. right after PATCH). Defaults
    from :func:`default_settings` are not pushed into the environment so ``.env``
    and existing process env stay authoritative.
    """
    saved = load_saved_settings()
    from_patch = settings is not None
    if settings is None:
        if not saved:
            return
        s = load_app_settings()
    else:
        s = settings
    apply_saved = bool(saved) or from_patch
    if not apply_saved:
        return

    ads = s.get("ads") or {}
    _set_env_if_unset("ADS_API_TOKEN", str(ads.get("api_token") or ""))

    llm = s.get("llm") or {}
    provider = str(llm.get("provider") or "").strip().lower()
    if provider:
        _set_env_if_unset("RESEARCH_LLM_PROVIDER", provider)
        _set_env_if_unset("QF_LLM_PROVIDER", provider)
    _set_env_if_unset("QF_LLM_API_KEY", str(llm.get("api_key") or ""))
    _set_env_if_unset("QF_LLM_BASE_URL", str(llm.get("base_url") or ""))
    _set_env_if_unset("QF_LLM_MODEL", str(llm.get("model") or ""))
    if provider in ("minimax", "mini_max"):
        _set_env_if_unset("MINIMAX_API_KEY", str(llm.get("api_key") or ""))
        _set_env_if_unset("MINIMAX_BASE_URL", str(llm.get("base_url") or ""))
        _set_env_if_unset("MINIMAX_MODEL", str(llm.get("model") or ""))

    emb = s.get("embedding") or {}
    emb_provider = str(emb.get("provider") or "").strip().lower()
    if emb_provider and apply_saved:
        _set_env_if_unset("RESEARCH_EMBEDDING_PROVIDER", emb_provider)
    model = str(emb.get("model") or "").strip()
    local_model = str(emb.get("local_model") or "").strip()
    hf_home = str(emb.get("hf_home") or "").strip()
    hf_offline = str(emb.get("hf_offline") or "").strip().lower()
    if emb_provider == "local_sentence_transformer":
        _set_env_if_unset("RESEARCH_LOCAL_EMBEDDING_MODEL", local_model or model)
        _set_env_if_unset("RESEARCH_LOCAL_EMBEDDING_DEVICE", str(emb.get("device") or "cpu"))
        if hf_home:
            if (Path(hf_home).expanduser() / ".cache" / "huggingface").is_dir():
                _set_env_if_unset("RESEARCH_LOCAL_EMBEDDING_HOME", hf_home)
            else:
                _set_env_if_unset("RESEARCH_LOCAL_EMBEDDING_HF_HOME", hf_home)
        if hf_offline in ("1", "true", "yes", "on"):
            _set_env_if_unset("RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE", "1")
    else:
        if model:
            _set_env_if_unset("RESEARCH_EMBEDDING_MODEL", model)
        _set_env_if_unset("RESEARCH_OPENAI_API_KEY", str(emb.get("api_key") or ""))
        _set_env_if_unset("RESEARCH_OPENAI_BASE_URL", str(emb.get("base_url") or ""))

    zot = s.get("zotero") or {}
    _set_env_if_unset("ZOTERO_LIBRARY_ID", str(zot.get("library_id") or ""))
    _set_env_if_unset("ZOTERO_API_KEY", str(zot.get("api_key") or ""))
    _set_env_if_unset("ZOTERO_LIBRARY_TYPE", str(zot.get("library_type") or "user"))


def configured_status(settings: Dict[str, Any] | None = None) -> Dict[str, bool]:
    s = settings if settings is not None else load_app_settings()
    ads = s.get("ads") or {}
    llm = s.get("llm") or {}
    emb = s.get("embedding") or {}
    zot = s.get("zotero") or {}

    llm_ok = bool(str(llm.get("api_key") or "").strip())
    emb_provider = str(emb.get("provider") or "").strip().lower()
    if emb_provider == "local_sentence_transformer":
        emb_ok = bool(
            str(emb.get("local_model") or emb.get("model") or "").strip()
        ) and bool(
            str(emb.get("hf_home") or "").strip()
            or str(os.environ.get("RESEARCH_LOCAL_EMBEDDING_HOME") or "").strip()
            or str(os.environ.get("RESEARCH_LOCAL_EMBEDDING_HF_HOME") or "").strip()
        )
    else:
        emb_ok = bool(str(emb.get("api_key") or "").strip())

    return {
        "ads": bool(str(ads.get("api_token") or "").strip()),
        "llm": llm_ok,
        "embedding": emb_ok,
        "zotero": bool(
            str(zot.get("library_id") or "").strip()
            and str(zot.get("api_key") or "").strip()
        ),
    }


def validate_patch(patch: Dict[str, Any]) -> None:
    llm = patch.get("llm")
    if isinstance(llm, dict):
        p = str(llm.get("provider") or "").strip().lower()
        if p and p not in _LLM_PROVIDERS:
            raise ValueError(f"invalid llm.provider: {p}")
    emb = patch.get("embedding")
    if isinstance(emb, dict):
        p = str(emb.get("provider") or "").strip().lower()
        if p and p not in _EMBEDDING_PROVIDERS:
            raise ValueError(f"invalid embedding.provider: {p}")


def merge_patch_with_secrets(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve existing secret values when PATCH sends empty strings."""
    current = load_app_settings()
    merged = deepcopy(patch)
    for section in ("ads", "llm", "embedding", "zotero"):
        cur_block = current.get(section) or {}
        new_block = merged.get(section)
        if not isinstance(new_block, dict):
            continue
        for k in ("api_token", "api_key"):
            if k in new_block and not str(new_block.get(k) or "").strip():
                if cur_block.get(k):
                    new_block[k] = cur_block[k]
    return merged


__all__ = [
    "apply_to_environ",
    "configured_status",
    "default_settings",
    "load_app_settings",
    "load_saved_settings",
    "mask_secrets",
    "merge_patch_with_secrets",
    "save_app_settings",
    "settings_file_exists",
    "settings_path",
    "validate_patch",
]
