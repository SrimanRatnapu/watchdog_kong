"""Send a steady stream of Watchdog-shaped model calls through Kong, so the Grafana dashboard has something to show.

Synthetic traffic, meant for the offline stand-in (make kong-mock). Each simulated case runs Watchdog's real triage call
(so Kong's prompt guard sees the real triage prompt), then agent turns and a photo exam through llm.py with the case id,
so the audit plugin files them by case. A share of the listings come from the red-team set, so some calls are blocked.

    cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_demo_traffic.py --minutes 5
Refuses a real Anthropic upstream unless --allow-spend.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DL_USAGE_FILE", str(REPO / "backend" / ".usage.kong_demo.json"))

from deadlang import config, llm, triage  # noqa: E402
from deadlang.schemas import Listing  # noqa: E402

MOCK_HOSTS = {"127.0.0.1", "localhost", "mock-anthropic", "host.docker.internal"}
OBJ = {"type": "object"}


def listings() -> tuple[list[Listing], list[Listing]]:
    real = [Listing(**json.loads(l)) for l in (REPO / "data" / "listings.jsonl").read_text().splitlines()]
    attacks = []
    for line in (REPO / "data" / "redteam" / "injections.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "attack":
            attacks.append(Listing(id=r["id"], marketplace="reconstructed", seller_id="S_RT", title=r["title"],
                                   description=r["description"], price_usd=900.0, category="Antiques",
                                   stated_material="bone", item_type="netsuke", ship_from="US-CA",
                                   posted_at="2026-10-25T00:00:00+00:00", is_reconstructed=True))
    return real, attacks


async def one_case(n: int, listing: Listing) -> str:
    case = f"C-DEMO-{n:04d}"
    t = await triage.screen(listing, case_id=case)
    if not t.suspicious:
        return "screened out"
    text = f"{listing.title}\n{listing.description}"
    photos = [REPO / "data" / p for p in listing.image_paths][:2]
    if photos:
        await llm.call_vision_json(model=config.MODEL_STRONG, system="Material exam (demo traffic).",
                                   user="Examine the photos.", image_paths=photos, schema_name="report_material",
                                   schema=OBJ, case_id=case)
    for rnd in range(random.randint(1, 3)):                       # the three agents, a few rounds
        await asyncio.gather(*(llm.call_json(model=config.MODEL_STRONG, system=f"{agent} (demo traffic)",
                                             user=f"Round {rnd + 1}. Evidence board:\n{text * 3}",
                                             schema_name="post_position", schema=OBJ, max_tokens=400, case_id=case)
                               for agent in ("undercover", "counsel", "case_officer")))
    if listing.language != "en":
        await llm.call_json(model=config.MODEL_TRIAGE, system="Translate (demo traffic).", user=text,
                            schema_name="report_translation", schema=OBJ, max_tokens=300, case_id=case)
    return "blocked at triage, reviewed" if t.reason == triage.GUARD_REASON else "reviewed"


async def main(minutes: float, per_wave: int, wave_seconds: float, attack_share: float, admin: str,
               allow_spend: bool) -> None:
    if not config.GATEWAY_URL:
        raise SystemExit("set DL_GATEWAY_URL to the Kong proxy (http://localhost:8010)")
    with urllib.request.urlopen(f"{admin}/services/anthropic", timeout=5) as r:
        host = json.load(r)["host"]
    if host not in MOCK_HOSTS and not allow_spend:
        raise SystemExit(f"Kong sends requests to {host}, which looks like the real API. Use make kong-mock.")
    real, attacks = listings()
    rng = random.Random(7)
    end, n, tally = time.monotonic() + minutes * 60, 0, {}
    while time.monotonic() < end:
        batch = []
        started = time.monotonic()
        for _ in range(per_wave):
            n += 1
            pick = rng.choice(attacks) if rng.random() < attack_share else rng.choice(real)
            batch.append(one_case(n, pick))
        for outcome in await asyncio.gather(*batch):
            tally[outcome] = tally.get(outcome, 0) + 1
        print(f"{n} cases so far: {tally}", flush=True)
        await asyncio.sleep(max(0.0, wave_seconds - (time.monotonic() - started)))
    print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--per-wave", type=int, default=4, help="cases started together")
    ap.add_argument("--wave-seconds", type=float, default=10, help="a new wave at most this often")
    ap.add_argument("--attack-share", type=float, default=0.15)
    ap.add_argument("--admin", default="http://127.0.0.1:8011")
    ap.add_argument("--allow-spend", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.minutes, a.per_wave, a.wave_seconds, a.attack_share, a.admin, a.allow_spend))
