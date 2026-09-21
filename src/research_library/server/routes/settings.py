"""App settings API: read/write API keys stored in data_dir/app_settings.json."""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from research_library.app_settings import (
    apply_to_environ,
    configured_status,
    load_app_settings,
    mask_secrets,
    merge_patch_with_secrets,
    save_app_settings,
    validate_patch,
)
from research_library.settings import reload_settings

router = APIRouter(tags=["settings"])


class SettingsPatch(BaseModel):
    ads: Dict[str, str] | None = None
    llm: Dict[str, str] | None = None
    embedding: Dict[str, str] | None = None
    zotero: Dict[str, str] | None = None


@router.get("/settings")
def get_settings() -> Dict[str, Any]:
    raw = load_app_settings()
    return {
        "settings": mask_secrets(raw),
        "configured": configured_status(raw),
    }


@router.patch("/settings")
def patch_settings(body: SettingsPatch) -> Dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raw = load_app_settings()
        return {"settings": mask_secrets(raw), "configured": configured_status(raw)}
    try:
        validate_patch(patch)
        merged_patch = merge_patch_with_secrets(patch)
        saved = save_app_settings(merged_patch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    apply_to_environ(saved)
    reload_settings()
    return {"settings": mask_secrets(saved), "configured": configured_status(saved)}


@router.post("/settings/test")
def test_settings() -> Dict[str, Any]:
    """Lightweight connectivity checks using current env (after app_settings apply)."""
    results: Dict[str, str] = {}

    ads_token = (os.environ.get("ADS_API_TOKEN") or "").strip()
    if not ads_token:
        results["ads"] = "missing token"
    else:
        try:
            from research_library.lookup import ads_query

            ads_query("bibcode:2021ApJS..255...20A", rows=1, fl=["bibcode"])
            results["ads"] = "ok"
        except Exception as e:
            results["ads"] = str(e)[:200]

    try:
        from research_library.analysis.llm.registry import get_chat_client

        llm = get_chat_client()
        from research_library.analysis.llm.base import ChatMessage

        llm.chat(
            [ChatMessage(role="user", content="ping")],
            max_completion_tokens=8,
            temperature=0,
        )
        results["llm"] = "ok"
    except Exception as e:
        results["llm"] = str(e)[:200]

    emb_provider = (os.environ.get("RESEARCH_EMBEDDING_PROVIDER") or "").strip().lower()
    try:
        from research_library.analysis.embeddings.registry import get_embedding_client

        client = get_embedding_client()
        client.embed(["ping"], input_type="query")
        results["embedding"] = "ok"
    except Exception as e:
        results["embedding"] = str(e)[:400]

    zid = (os.environ.get("ZOTERO_LIBRARY_ID") or "").strip()
    zkey = (os.environ.get("ZOTERO_API_KEY") or "").strip()
    if zid and zkey:
        results["zotero"] = "configured"
    else:
        results["zotero"] = "missing credentials"

    return {"results": results}
