"""L0 segmentation + normalize — deterministic, no network."""
from deepvalue.ingest.normalize import split_sentences, to_sentences
from deepvalue.ingest.segmentation import extract_mdna, segment_mdna


def test_sentence_split_protects_abbreviations_and_decimals():
    s = split_sentences("Inc. shipped 1.5M units. Revenue rose. The covenant was waived.")
    assert s == ["Inc. shipped 1.5M units.", "Revenue rose.", "The covenant was waived."]


def test_to_sentences_stable_ids():
    out = to_sentences("First sentence here. Second one follows.")
    assert [x["sentence_id"] for x in out] == ["s_0000", "s_0001"]
    assert out[0]["text"].startswith("First")


# Synthetic 10-K: a TOC listing Item 7/7A/8, then the real body. The extractor must return the
# BODY MD&A (bounded at body Item 7A) — NOT the TOC-start -> body over-capture, and NOT empty.
_BODY = ("Item 7. Management's Discussion and Analysis of Financial Condition and Results of "
         "Operations. " + "Our revenue and results of operations improved this year. " * 60)
_HTML = (
    "<html><body>"
    "Item 7. Management's Discussion and Analysis 24 "
    "Item 7A. Quantitative and Qualitative Disclosures 30 "
    "Item 8. Financial Statements 33 "                       # <- table of contents
    + "filler " * 200
    + _BODY
    + "Item 7A. Quantitative and Qualitative Disclosures About Market Risk. None. "
    + "Item 8. Financial Statements and Supplementary Data. See the consolidated statements."
    + "</body></html>"
)


def test_extract_mdna_picks_body_not_toc_and_does_not_overcapture():
    seg = extract_mdna(_HTML, "10-K")
    assert seg is not None
    assert "results of operations improved" in seg.lower()          # got the body
    assert "Financial Statements and Supplementary" not in seg      # stopped at Item 7A (no over-capture)


def test_segment_mdna_reports_method_and_sentences():
    sec = segment_mdna(_HTML, "10-K")
    assert sec.method == "heuristic" and sec.canonical_id == "10-K.mdna"
    assert len(sec.sentences) > 5 and sec.sentences[0]["sentence_id"] == "s_0000"


def test_no_mdna_returns_none_method():
    sec = segment_mdna("<html>nothing relevant here</html>", "10-K")
    assert sec.text is None and sec.method == "none"


_NOTES = (
    "Note 5. Related Party Transactions. " + "The CEO leases a building from a related entity. " * 20
    + "Note 6. Income Taxes. " + "The deferred tax asset valuation allowance increased. " * 20
)


def test_extract_footnote_finds_topic_bounded_by_next_note():
    from deepvalue.ingest.segmentation import extract_footnote
    rp = extract_footnote(_NOTES, "related_party", is_html=False)
    assert rp is not None and "leases a building from a related entity" in rp
    assert "Income Taxes" not in rp        # bounded at 'Note 6' — didn't bleed into the next note


def test_extract_footnote_unknown_topic_or_absent():
    from deepvalue.ingest.segmentation import extract_footnote
    assert extract_footnote(_NOTES, "nonsense_topic", is_html=False) is None
    assert extract_footnote("no notes here", "debt", is_html=False) is None
