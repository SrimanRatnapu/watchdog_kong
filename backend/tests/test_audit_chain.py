"""scripts/verify_audit_chain.py: the Python side of the watchdog-audit Kong plugin's hash chain."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_chain.py"
_spec = importlib.util.spec_from_file_location("verify_audit_chain", SCRIPT)
vac = importlib.util.module_from_spec(_spec)
sys.modules["verify_audit_chain"] = vac
_spec.loader.exec_module(vac)

# Pinned in infra/kong/plugins/watchdog-audit/chain_test.go too: Go and Python hash the same bytes.
PINNED_HASH = "e6744b669696d770f8c49184552a26d051c6f4f1cbde7b3ac6a5e5c635fd3868"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def entry(seq: int, prev: str, case: str = "C-0001", status: int = 200, consumer: str = "agents") -> dict:
    e = {"seq": seq, "at": "2026-10-25T00:00:00Z", "consumer": consumer, "case_id": case, "model": "claude-sonnet-5-5",
         "status": status, "source": "service", "blocked": False, "request_sha256": sha(b"req"),
         "response_sha256": sha(b"resp"), "prev": prev}
    e["hash"] = vac.entry_hash(e)
    return e


def chain(n: int) -> list[str]:
    out, prev = [], vac.GENESIS
    for i in range(1, n + 1):
        e = entry(i, prev, consumer="triage" if i == 1 else "agents")
        out.append(json.dumps(e))
        prev = e["hash"]
    return out


def test_hash_matches_the_go_plugin():
    e = entry(1, vac.GENESIS, case="C-<&>-ñ")
    assert e["hash"] == PINNED_HASH


def test_a_good_chain_verifies_and_lists_a_case(tmp_path, capsys):
    p = tmp_path / "chain.jsonl"
    p.write_text("\n".join(chain(3)) + "\n")
    assert vac.main([str(p), "--case", "C-0001"]) == 0
    out = capsys.readouterr().out
    assert "verified: 3 entries" in out and "case C-0001: 3 calls" in out and "agents 2, triage 1" in out


def test_every_kind_of_edit_is_caught():
    lines = chain(4)
    changed = lines.copy()
    changed[1] = changed[1].replace('"status": 200', '"status": 201')
    rehashed = json.loads(lines[1])
    rehashed["case_id"] = "C-9999"
    rehashed["hash"] = vac.entry_hash(rehashed)          # a consistent entry, but the next one's prev no longer matches
    for edited, where in ((changed, "line 2"), (lines[:1] + lines[2:], "line 2"), ([lines[1], lines[0]] + lines[2:], "line 1"),
                          (lines[:1] + [json.dumps(rehashed)] + lines[2:], "line 3")):
        good, problem = vac.verify(edited)
        assert problem and problem.startswith(where), problem


def test_a_broken_chain_exits_1(tmp_path):
    p = tmp_path / "chain.jsonl"
    p.write_text("\n".join(chain(2)[1:]) + "\n")           # first entry missing
    assert vac.main([str(p)]) == 1
