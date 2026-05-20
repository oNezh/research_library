"""Load project `.env` (same as runtime) before tests collect."""

from __future__ import annotations

from research_library.config import load_env

load_env()
