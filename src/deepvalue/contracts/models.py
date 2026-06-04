"""
Data contracts — the SINGLE SOURCE OF TRUTH (spec §12).

Every layer reads/writes these. If prose and a contract disagree, the contract wins.
Point-in-time rule baked in: use `filing_date` for as-of logic, NEVER `period_of_report`.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class Filing(BaseModel):
    cik: str
    ticker: str
    accession_no: str
    form_type: str
    filing_date: date            # USE THIS for point-in-time, never period_of_report
    period_of_report: date
    primary_doc_url: str


class FilingSection(BaseModel):
    filing_id: str               # accession_no
    canonical_id: str            # e.g. "10-K.item_1a", "footnote.related_party"
    title: str
    normalized_text: str
    sentences: list[dict]        # [{ "sentence_id": "s_042", "text": "..." }]
    seg_confidence: float


class QuantScreenResult(BaseModel):
    ticker: str
    cik: str
    as_of: date
    screen_profile: Literal["net_net", "normalized_earnings", "hidden_assets"]
    value_metrics: dict          # ncav, ev_ebit, p_tbv, ...
    trap_signals: dict           # z_score, m_score, f_score, runway_months, going_concern
    passed: bool
    rank: int


class TriageDecision(BaseModel):
    ticker: str
    proceed: bool
    reason: str
    cache_hit: bool
    sections_changed: list[str]


class DiffFinding(BaseModel):
    ticker: str
    section: str
    change_type: Literal["added", "deleted", "modified"]
    category: str                # removed_reassurance, covenant, liquidity, ...
    materiality: float           # 0-1
    old_span: Optional[str] = None
    new_span: Optional[str] = None
    citation: list[str]          # sentence_ids
    rationale: str


class ForensicFinding(BaseModel):
    ticker: str
    agent: Literal["footnote", "asset", "capital_structure"]
    finding_type: str
    severity: float              # 0-1
    impairs_book_value: bool
    est_impact_usd: Optional[float] = None   # computed via code interpreter
    citation: list[str]
    rationale: str
    requires_rebuttal: bool


class Objection(BaseModel):
    id: str
    type: str                    # routes to a specialist
    claim: str
    routed_to: str
    status: Literal["open", "rebutted", "sustained"]
    evidence: list[str]          # citations


class ThesisVerdict(BaseModel):
    ticker: str
    as_of: date
    decision: Literal["BUY", "WATCH", "PASS"]
    conviction: float            # 0-1, calibrated against realized Brier
    margin_of_safety: float      # discount to conservative asset value
    surviving_risks: list[str]
    dependencies: list[str]
    unresolved_objections: list[Objection]
    bull_summary: str


class OutcomeRecord(BaseModel):
    verdict_id: str
    ticker: str
    decision: str
    conviction: float
    as_of: date
    outcome_events: list[str]
    forward_returns: dict        # {"6m": .., "12m": .., "24m": ..}
    realized: bool


class AgentCalibration(BaseModel):
    target: str                  # agent or flag_type
    precision: float
    recall: float
    brier: Optional[float] = None
    sample_n: int
    weight: float                # fed back into L4/L5
