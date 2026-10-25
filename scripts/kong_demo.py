"""A two-minute live walkthrough of Watchdog behind Kong, for a screen share.

Needs the offline stack (make kong-mock; add --profile observability for Grafana) and DL_GATEWAY_URL in .env.
    cd backend && uv run python ../scripts/kong_demo.py          # add --step to pause for Enter between steps
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
os.environ.setdefault("DL_USAGE_FILE", str(REPO / "backend" / ".usage.kong_demo.json"))

from deadlang import config, triage  # noqa: E402
from deadlang.schemas import Listing  # noqa: E402

ADMIN = "http://127.0.0.1:8011"
CHAIN = REPO / "infra" / "kong" / "audit" / "chain.jsonl"
VERIFY = REPO / "scripts" / "verify_audit_chain.py"
B, DIM, RED, GREEN, CYAN, END = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[36m", "\033[0m"


def say(title: str, step: bool) -> None:
    if step:
        input(f"{DIM}[Enter]{END} ")
    print(f"\n{B}{CYAN}▶ {title}{END}")


def metrics() -> str:
    with urllib.request.urlopen(f"{ADMIN}/metrics", timeout=5) as r:
        return r.read().decode()


def tokens_by_role(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in re.finditer(r'^kong_ai_llm_tokens_total\{[^}]*token_type="total_tokens"[^}]*consumer="([^"]*)"[^}]*\} (\S+)', text, re.M):
        out[m.group(1)] = out.get(m.group(1), 0) + int(float(m.group(2)))
    return out


def run_verify(path: Path, case: str | None = None) -> str:
    args = [sys.executable, str(VERIFY), str(path)] + (["--case", case] if case else [])
    return subprocess.run(args, capture_output=True, text=True).stdout.rstrip()


async def main(step: bool) -> None:
    if not config.GATEWAY_URL:
        raise SystemExit("set DL_GATEWAY_URL=http://localhost:8010 in .env first (docs/kong.md)")
    with urllib.request.urlopen(f"{ADMIN}/plugins", timeout=5) as r:
        plugins = sorted({p["name"] for p in json.load(r)["data"]})

    say("1. Every model call Watchdog makes now goes through Kong", step)
    print(f"   gateway {config.GATEWAY_URL}   plugins: {', '.join(plugins)}")
    print(f"   Watchdog holds one gateway key per role; only Kong holds the Anthropic key.")

    extra = json.loads((REPO / "data" / "extra" / "listings.jsonl").read_text().splitlines()[0])["listing"]
    normal = Listing(**json.loads((REPO / "data" / "listings.jsonl").read_text().splitlines()[0]))
    attack = Listing(**extra)

    say("2. A normal listing: Kong passes it to the model", step)
    print(f'   "{normal.title}"  (${normal.price_usd:,.0f})')
    r = await triage.screen(normal, case_id="C-LIVE-1")
    print(f"   triage score {r.score:.2f}  {GREEN}passed the guard{END}")

    say("3. A listing whose text tries to instruct the AI", step)
    hidden = attack.description[attack.description.index("NOTICE"):][:110]
    print(f'   "...{hidden}..."')
    r = await triage.screen(attack, case_id="C-LIVE-2")
    print(f"   {RED}Kong's ai-prompt-guard blocked it before any model saw it{END}")
    print(f"   Watchdog's triage: score {r.score:.1f}, suspicious={r.suspicious}")
    print(f"   {DIM}{r.reason}{END}")

    say("4. Kong counts tokens per role (Prometheus, the Grafana dashboard reads this)", step)
    for role, n in sorted(tokens_by_role(metrics()).items()):
        print(f"   {role:<12} {n:>10,} tokens")
    print(f"   Grafana: http://localhost:3001")

    say("5. Our Go plugin, watchdog-audit, logged every call in a hash chain", step)
    print("   " + run_verify(CHAIN, "C-LIVE-2").replace("\n", "\n   "))

    say("6. Someone edits one entry after the fact", step)
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "chain.jsonl"
        shutil.copy(CHAIN, copy)
        lines = copy.read_text().splitlines()
        i = next(k for k, l in enumerate(lines) if '"case_id":"C-LIVE-2"' in l)
        lines[i] = lines[i].replace('"blocked":true', '"blocked":false').replace('"status":400', '"status":200')
        copy.write_text("\n".join(lines) + "\n")
        print(f"   changed entry #{i + 1}: blocked=true -> false, status 400 -> 200")
        print(f"   {RED}" + run_verify(copy).replace("\n", f"\n   ") + END)
    print(f"\n{B}Done.{END} Details: docs/kong.md")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--step", action="store_true", help="wait for Enter before each step")
    asyncio.run(main(ap.parse_args().step))
