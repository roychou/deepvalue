"""
Pinned model registry — the single source of truth for the specialist models.

Model drift is a silent killer: upgrading a model changes the validated behavior AND
shifts the training-data contamination cutoff AND the pricing. Pinning every model ID,
its training-data cutoff, and its list price in one place makes a model change a
*deliberate edit here* — and the signal to re-run the validation suite
(productization.md 3.1). Treat editing this file like a deploy.

Downstream (budget pricing, temporal cutoffs, the specialists) reads from here, so
there's no second copy to drift. Each run records these IDs (runlog) for provenance.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str                  # Anthropic API model ID (a pinned snapshot)
    training_cutoff: str     # YYYY-MM-DD, training-DATA cutoff (conservative; for contamination)
    input_per_mtok: float    # USD per 1M input tokens (list price)
    output_per_mtok: float   # USD per 1M output tokens


# ROOT makes the judgments (fundamentals/technicals + sentiment/news synthesis);
# LEAF does the cheap sentiment/news map-reduce leaf step. Cutoffs/prices verified
# against the Claude models overview (see notes/productization.md 0.0).
ROOT = ModelSpec("claude-sonnet-4-6", "2026-01-31", 3.0, 15.0)
LEAF = ModelSpec("claude-haiku-4-5-20251001", "2025-07-31", 1.0, 5.0)

REGISTRY: dict[str, ModelSpec] = {ROOT.id: ROOT, LEAF.id: LEAF}

# The binding contamination boundary is the LATEST training cutoff among the models
# that make decisions — ROOT drives every judgment, so its cutoff governs.
DECISION_MODEL_CUTOFF = ROOT.training_cutoff


# === Model-cutoff LADDER (forward-validation-design.md) ===========================
# Alternative decision models with EARLIER training cutoffs — used to manufacture longer
# contamination-free windows (older cutoff → more clean dates) so the IC harness gets a
# real date count. NOT deployed: the live bot stays on ROOT/LEAF; these are swapped in only
# for offline ladder runs. Using training-DATA cutoff (conservative — weights can encode
# anything up to it, regardless of the later "reliable knowledge" date).
#
# VERIFIED against platform.claude.com models overview, fetched 4 Jun 2026. Claude 3.x is
# fully RETIRED (unavailable) — the realistic ladder floor is the Claude-4.0 generation at a
# Mar-2025 cutoff. Re-verify before relying on these (cutoffs are operator-supplied; guessing
# wrong silently re-contaminates — see temporal.py).
#
# ⚠ TIME-SENSITIVE: claude-sonnet-4-0 / claude-opus-4-0 are DEPRECATED and RETIRE 15 Jun 2026.
# Sonnet 4.0 is the cheap deep rung (Mar-2025 cutoff, $3/$15) but only available ~11 more days
# from this note. The durable Mar-2025 rung after that is Opus 4.1 (same cutoff, but $15/$75).
LADDER_MODELS: dict[str, ModelSpec] = {
    # id                                    training_cutoff   in    out     ~clean wk dates / notes
    "claude-sonnet-4-20250514":  ModelSpec("claude-sonnet-4-20250514",  "2025-03-31",  3.0, 15.0),   # ~55; CHEAP+DEEP; retires 2026-06-15
    "claude-opus-4-1-20250805":  ModelSpec("claude-opus-4-1-20250805",  "2025-03-31", 15.0, 75.0),   # ~55; durable Mar-2025 rung; pricey
    "claude-sonnet-4-5-20250929": ModelSpec("claude-sonnet-4-5-20250929", "2025-07-31",  3.0, 15.0),  # ~38; cheap, not retiring
    "claude-opus-4-20250514":    ModelSpec("claude-opus-4-20250514",    "2025-03-31", 15.0, 75.0),   # ~55; retires 2026-06-15
}


def price_table() -> dict[str, tuple[float, float]]:
    """{model_id: (input_$/Mtok, output_$/Mtok)} for the spend meter. Includes the
    dateless Haiku alias so either form prices correctly, plus the ladder models so an
    offline ladder run still meters spend correctly."""
    table = {m.id: (m.input_per_mtok, m.output_per_mtok) for m in REGISTRY.values()}
    table["claude-haiku-4-5"] = (LEAF.input_per_mtok, LEAF.output_per_mtok)  # alias
    for m in LADDER_MODELS.values():
        table.setdefault(m.id, (m.input_per_mtok, m.output_per_mtok))
    return table


def pinned_ids() -> list[str]:
    return [ROOT.id, LEAF.id]
