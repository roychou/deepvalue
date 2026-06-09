"""L4/L5 pure logic — coercion + judge escalation. No SDK / network."""
from datetime import date

from deepvalue.agents.harness import BudgetMeter, coerce_finding, coerce_objection, parse_json
from deepvalue.agents.subagents.judge import NEEDS_REVIEW, _review_required, needs_review
from deepvalue.contracts.models import Objection, ThesisVerdict


def test_parse_json_handles_prose_and_decoys():
    assert parse_json('analysis... final: [{"a":1}] done') == [{"a": 1}]
    assert parse_json('see [Item 8]; result {"decision":"PASS"} end') == {"decision": "PASS"}


def test_coercion_binds_agent_schemas():
    f = coerce_finding({"type": "covenant", "risk": 0.9, "finding": "in default"}, "AMS", "capital_structure")
    assert f.agent == "capital_structure" and f.severity == 0.9 and "default" in f.rationale
    o = coerce_objection({"objection": "covenant breach", "citation": ["mdna s_0200"]}, 0)
    assert o.claim.startswith("covenant") and o.evidence == ["mdna s_0200"]


def test_budget_meter_hard_total():
    b = BudgetMeter(2.0)
    b.add(1.5)
    assert not b.exhausted()
    b.add(0.6)
    assert b.exhausted()


def test_judge_failure_escalates_to_review_not_pass():
    objs = [Objection(id="O1", type="covenant_breach", claim="in default on all covenants",
                      routed_to="cap", status="open", evidence=["mdna s_0200"])]
    v = _review_required("AMS", "2026-06-08", "long thesis here", objs, "budget exhausted")
    assert v.decision == "WATCH"                  # NOT a silent PASS
    assert needs_review(v)                         # flagged for human intervention
    assert v.surviving_risks[0].startswith(NEEDS_REVIEW)
    assert v.unresolved_objections == objs         # evidence surfaced for the human


def test_clean_verdict_is_not_flagged_for_review():
    clean = ThesisVerdict(ticker="X", as_of=date(2026, 6, 8), decision="PASS", conviction=0.5,
                          margin_of_safety=0.4, surviving_risks=["debt load"], dependencies=[],
                          unresolved_objections=[], bull_summary="")
    assert not needs_review(clean)
