"""Measure the Kong prompt guard through the real gateway: what it blocks on the red-team set and on every stored listing.

Each listing goes through Watchdog's own triage call (triage.screen -> llm -> Kong), so the guard sees exactly the
prompt Watchdog sends. A listing counts as blocked when triage comes back with the guard's reason.

Run Kong against the offline stand-in, so passing listings cost nothing:
    python infra/kong/mock_anthropic.py 9998 &
    KONG_UPSTREAM_URL=http://127.0.0.1:9998 python infra/kong/render.py   # then start Kong (docs/kong.md)
    cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_guard_eval.py
The script asks Kong's admin API where it sends requests and refuses a real upstream unless --allow-spend is given.
Writes results/kong_guard.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DL_USAGE_FILE", str(REPO / "backend" / ".usage.kong_eval.json"))

from deadlang import config, triage  # noqa: E402
from deadlang.schemas import Listing  # noqa: E402

REDTEAM = REPO / "data" / "redteam" / "injections.jsonl"
STORED = (REPO / "data" / "listings.jsonl", REPO / "data" / "extra" / "listings.jsonl")
OUT = REPO / "results" / "kong_guard.json"
MOCK_HOSTS = {"127.0.0.1", "localhost", "mock-anthropic", "host.docker.internal"}


def upstream_host(admin: str) -> str:
    with urllib.request.urlopen(f"{admin}/services/anthropic", timeout=5) as r:
        return json.load(r)["host"]


def redteam_listing(row: dict) -> Listing:
    return Listing(id=row["id"], marketplace="reconstructed", seller_id="S_RT", title=row["title"],
                   description=row["description"], price_usd=900.0, category="Antiques", stated_material="bone",
                   item_type="netsuke", ship_from="US-CA", posted_at="2026-10-25T00:00:00+00:00", is_reconstructed=True)


def stored_listings() -> list[Listing]:
    out = []
    for path in STORED:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            out.append(Listing(**row.get("listing", row)))
    return out


async def blocked(listings: list[Listing], concurrency: int = 8) -> dict[str, bool]:
    sem = asyncio.Semaphore(concurrency)

    async def one(listing: Listing) -> tuple[str, bool]:
        async with sem:
            res = await triage.screen(listing)
            if res.reason.startswith(triage.COULD_NOT_RUN):
                raise RuntimeError(f"{listing.id}: {res.reason}")      # a broken gateway must not read as "passed"
            return listing.id, res.reason == triage.GUARD_REASON
    return dict(await asyncio.gather(*(one(l) for l in listings)))


def rate(count: int, n: int) -> dict:
    return {"count": count, "n": n, "text": f"{count} of {n}"}


async def main(admin: str, allow_spend: bool) -> None:
    if not config.GATEWAY_URL:
        raise SystemExit("set DL_GATEWAY_URL to the Kong proxy (http://localhost:8010)")
    host = upstream_host(admin)
    if host not in MOCK_HOSTS and not allow_spend:
        raise SystemExit(f"Kong sends requests to {host}, which looks like the real API. Use the mock, or --allow-spend.")

    rows = [json.loads(l) for l in REDTEAM.read_text().splitlines()]
    rt = await blocked([redteam_listing(r) for r in rows])
    stored = stored_listings()
    st = await blocked(stored)

    attacks = [r for r in rows if r["kind"] == "attack"]
    benign = [r for r in rows if r["kind"] == "benign"]
    by_cat: dict[str, dict] = {}
    for cat in sorted({r["category"] for r in attacks}):
        rs = [r for r in attacks if r["category"] == cat]
        by_cat[cat] = rate(sum(rt[r["id"]] for r in rs), len(rs))
    stored_hits = sorted(lid for lid, b in st.items() if b)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gateway": "Kong Gateway 3.12, ai-prompt-guard on the triage consumer (infra/kong/kong.template.yml)",
        "upstream": host,
        "redteam": {
            "attacks_blocked": rate(sum(rt[r["id"]] for r in attacks), len(attacks)),
            "attacks_blocked_by_category": by_cat,
            "attacks_missed": [r["id"] for r in attacks if not rt[r["id"]]],
            "benign_blocked": rate(sum(rt[r["id"]] for r in benign), len(benign)),
            "benign_blocked_ids": [r["id"] for r in benign if rt[r["id"]]],
        },
        "stored_listings": {
            "blocked": rate(len(stored_hits), len(stored)),
            "blocked_ids": stored_hits,
            "note": "L9001 is the team's own injection test listing (data/extra); every other stored listing is a "
                    "real eBay listing or a reconstructed ivory case with no injected text.",
        },
    }
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    r = result["redteam"]
    print(f"red team: blocked {r['attacks_blocked']['text']} attacks, {r['benign_blocked']['text']} benign look-alikes")
    for cat, v in by_cat.items():
        print(f"  {cat:12s} {v['text']}")
    print(f"stored listings: blocked {result['stored_listings']['blocked']['text']} ({', '.join(stored_hits) or 'none'})")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--admin", default="http://127.0.0.1:8011", help="Kong admin API")
    ap.add_argument("--allow-spend", action="store_true", help="allow a real Anthropic upstream (passing listings are billed)")
    a = ap.parse_args()
    asyncio.run(main(a.admin, a.allow_spend))
