"""Check the watchdog-audit chain written by the Kong plugin, and list the model calls behind a case.

Every entry's hash covers all its fields and the previous entry's hash, so an edited, removed or reordered entry is
reported at the first line that no longer matches. Stdlib only, so whoever receives a case file can run it.

    python scripts/verify_audit_chain.py infra/kong/audit/chain.jsonl
    python scripts/verify_audit_chain.py infra/kong/audit/chain.jsonl --case C-0007
Exit status 1 when the chain does not verify.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

GENESIS = "0" * 64


def entry_hash(entry: dict) -> str:
    """Same bytes as canonical() in infra/kong/plugins/watchdog-audit/chain.go."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(lines: list[str]) -> tuple[list[dict], str | None]:
    """(entries that verified, the first problem or None)."""
    out: list[dict] = []
    prev, seq = GENESIS, 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            return out, f"line {n}: not an entry"
        if e.get("seq") != seq + 1:
            return out, f"line {n}: seq {e.get('seq')}, expected {seq + 1}"
        if e.get("prev") != prev:
            return out, f"line {n} (seq {e['seq']}): prev does not match the hash of the entry before"
        if e.get("hash") != entry_hash(e):
            return out, f"line {n} (seq {e['seq']}): hash does not match the entry's contents"
        out.append(e)
        prev, seq = e["hash"], e["seq"]
    return out, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chain", type=Path)
    ap.add_argument("--case", help="list the calls for this case id")
    a = ap.parse_args(argv)
    entries, problem = verify(a.chain.read_text(encoding="utf-8").splitlines())
    if problem:
        print(f"NOT VERIFIED after {len(entries)} good entries: {problem}")
        return 1
    print(f"verified: {len(entries)} entries, chain head {entries[-1]['hash'][:16] if entries else GENESIS[:16]}")
    roles = Counter(e["consumer"] or "(none)" for e in entries)
    print("calls by role: " + ", ".join(f"{k} {v}" for k, v in sorted(roles.items())))
    blocked = [e for e in entries if e["blocked"]]
    print(f"blocked by the gateway: {len(blocked)}")
    if a.case:
        mine = [e for e in entries if e["case_id"] == a.case]
        print(f"\ncase {a.case}: {len(mine)} calls")
        for e in mine:
            print(f"  #{e['seq']:<5} {e['at'][:19]}  {e['consumer']:<11} {e['model']:<28} {e['status']}"
                  f"{'  BLOCKED' if e['blocked'] else ''}  req {e['request_sha256'][:12]}  resp {e['response_sha256'][:12] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
