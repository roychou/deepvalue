"""Model assignment per node (spec §13.4) — reads config/models.yaml so tiers swap without
code changes. Imported by both the harness and the subagents (depends on nothing in agents/,
so no import cycle)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_MODELS_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
_FALLBACK = "claude-opus-4-8"


@lru_cache(maxsize=1)
def _nodes() -> dict:
    try:
        return yaml.safe_load(_MODELS_PATH.read_text()).get("nodes", {})
    except Exception:  # noqa: BLE001 — config absent in some test contexts
        return {}


def model_for(node: str) -> str:
    """Model id for a pipeline node ('forensic', 'adversarial', 'triage', 'diff_materiality')."""
    return _nodes().get(node, _FALLBACK)
