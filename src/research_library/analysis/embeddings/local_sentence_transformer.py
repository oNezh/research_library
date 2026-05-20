"""In-process embeddings via ``sentence_transformers`` (e.g. Qwen3-Embedding-4B)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Sequence

from research_library.analysis.llm.base import LLMError
from research_library.config import load_env


def _env_truthy(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in ("1", "true", "yes")


def _apply_hf_hub_offline_flags() -> None:
    """Avoid network when cache is complete (see RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE)."""
    if _env_truthy("RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE"):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _first_local_snapshot(hf_home: Path, hub_model_id: str) -> Path | None:
    """Use cached snapshot dir so SentenceTransformer does not hit huggingface.co."""
    if Path(hub_model_id).is_dir():
        return Path(hub_model_id).resolve()
    hub_part = "models--" + hub_model_id.replace("/", "--")
    snaps = hf_home / "hub" / hub_part / "snapshots"
    if not snaps.is_dir():
        return None
    for child in sorted(snaps.iterdir()):
        if child.is_dir() and (child / "modules.json").is_file():
            return child.resolve()
    return None


class LocalSentenceTransformerEmbeddings:
    """HF / SentenceTransformer models loaded locally (GPU or CPU).

    For `Qwen/Qwen3-Embedding-4B`, set ``RESEARCH_LOCAL_EMBEDDING_HOME`` to the repo
    that contains ``.cache/huggingface`` (same layout as the ``qwen`` workspace), so
    ``HF_HOME`` points at that cache and weights load offline.
    """

    def __init__(
        self,
        model_name: str,
        *,
        hf_home: Path | None = None,
        device: str | None = None,
        trust_remote_code: bool = False,
        prompt_query: str | None = "query",
        prompt_document: str | None = None,
        normalize_embeddings: bool = False,
    ) -> None:
        self._model_name = model_name
        self._hf_home = hf_home
        self._device = (device or "").strip() or None
        self._trust_remote_code = trust_remote_code
        self._prompt_query = prompt_query
        self._prompt_document = (
            (prompt_document or "").strip() or None
        )
        self._normalize_embeddings = normalize_embeddings
        self._model: Any = None
        self.embedding_dim: int = 0

    def _ensure_hf_home(self) -> None:
        _apply_hf_hub_offline_flags()
        if self._hf_home is None:
            return
        p = self._hf_home.expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(p)

    def _lazy_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise LLMError(
                'Local embeddings need sentence-transformers. Install: pip install -e ".[semantic-local]"'
            ) from e
        _apply_hf_hub_offline_flags()
        self._ensure_hf_home()
        kwargs: dict[str, Any] = {"trust_remote_code": self._trust_remote_code}
        if self._device:
            kwargs["device"] = self._device
        self._model = SentenceTransformer(self._model_name, **kwargs)
        ged = getattr(self._model, "get_embedding_dimension", None)
        if callable(ged):
            self.embedding_dim = int(ged() or 0)
        else:
            legacy = getattr(self._model, "get_sentence_embedding_dimension", lambda: 0)
            self.embedding_dim = int(legacy() or 0)
        return self._model

    def embed_texts(
        self,
        texts: List[str],
        *,
        model: str | None = None,
        for_query: bool = False,
    ) -> List[List[float]]:
        if model and model.strip() and model.strip() != self._model_name:
            raise LLMError(
                f"Local embedding model is fixed to {self._model_name!r}; got override {model!r}"
            )
        m = self._lazy_model()
        if for_query:
            pn = self._prompt_query
        else:
            pn = self._prompt_document
        encode_kw: dict[str, Any] = {
            "show_progress_bar": False,
            "convert_to_numpy": True,
        }
        if pn:
            encode_kw["prompt_name"] = pn
        if self._normalize_embeddings:
            encode_kw["normalize_embeddings"] = True
        vecs = m.encode(list(texts), **encode_kw)
        rows: Sequence[Any] = vecs
        out: List[List[float]] = []
        for row in rows:
            out.append([float(x) for x in row])
        if out:
            self.embedding_dim = len(out[0])
        return out

    @classmethod
    def from_env(cls) -> LocalSentenceTransformerEmbeddings:
        load_env()
        _apply_hf_hub_offline_flags()
        home_raw = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_HOME") or "").strip()
        hf_home_raw = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_HF_HOME") or "").strip()
        if hf_home_raw:
            hf_home = Path(hf_home_raw).expanduser().resolve()
        elif home_raw:
            hf_home = Path(home_raw).expanduser().resolve() / ".cache" / "huggingface"
        else:
            raise LLMError(
                "Local embeddings: set RESEARCH_LOCAL_EMBEDDING_HOME (qwen repo root, "
                "contains .cache/huggingface) or RESEARCH_LOCAL_EMBEDDING_HF_HOME (HF_HOME path)"
            )
        model = (
            (os.environ.get("RESEARCH_LOCAL_EMBEDDING_MODEL") or "").strip()
            or "Qwen/Qwen3-Embedding-4B"
        )
        if _env_truthy("RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE") or _env_truthy("HF_HUB_OFFLINE"):
            snap = _first_local_snapshot(hf_home, model)
            if snap is not None:
                model = str(snap)
        device = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_DEVICE") or "").strip() or None
        trc = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_TRUST_REMOTE_CODE") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        pq = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_PROMPT_QUERY") or "").strip()
        prompt_query = pq if pq else "query"
        pd_ = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_PROMPT_DOCUMENT") or "").strip()
        prompt_document = pd_ if pd_ else None
        norm = (os.environ.get("RESEARCH_LOCAL_EMBEDDING_NORMALIZE") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(
            model,
            hf_home=hf_home,
            device=device,
            trust_remote_code=trc,
            prompt_query=prompt_query,
            prompt_document=prompt_document,
            normalize_embeddings=norm,
        )
