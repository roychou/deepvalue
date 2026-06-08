"""
Build the persistent CIK->SIC map (reference/cik_sic.json) for the active universe, once,
GENTLY — so the v0 funnel's sector exclusion (banks/insurers) works without re-hitting SEC
hundreds of times per run (which trips SEC's ~10-min rate block).

Resumable: skips CIKs already in the map. Extra-polite pacing on top of edgar's throttle
since we've been heavy on SEC. Run anytime; safe to interrupt.

    uv run python scripts/build_sic_map.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from deepvalue.ingest.edgar import _load_sic_map, company_sic, flush_sic_map  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sic_build")


def main() -> None:
    manifest = json.loads((ROOT / "data/cache/manifest.json").read_text())
    ciks = sorted({m["cik"] for m in manifest
                   if m.get("status") == "active" and m.get("cik") and m.get("n_price", 0) > 0})
    smap = _load_sic_map()
    todo = [c for c in ciks if str(c).zfill(10) not in smap]
    log.info("active CIKs=%d | already mapped=%d | to fetch=%d", len(ciks), len(ciks) - len(todo), len(todo))

    done = 0
    for cik in todo:
        company_sic(cik)            # populates the persistent map (or leaves unmapped on SEC fail)
        done += 1
        time.sleep(0.25)            # extra-polite on top of edgar's 0.15s gate (~2.5 req/s total)
        if done % 200 == 0:
            flush_sic_map()
            log.info("  %d/%d fetched | map size=%d", done, len(todo), len(_load_sic_map()))
    flush_sic_map()
    log.info("DONE | SIC map size=%d", len(_load_sic_map()))


if __name__ == "__main__":
    main()
