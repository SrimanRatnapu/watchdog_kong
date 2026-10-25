"""Settings from the environment (and the repo-root .env). Defaults match .env.example.

Relative paths in DL_DATA_DIR / DL_USAGE_FILE resolve against backend/ (where the Procfile runs every
process), so all processes share one data dir and one usage file whatever their cwd.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

load_dotenv(REPO_ROOT / ".env", override=False)


def _str(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    value = raw.split(" #", 1)[0].strip()   # tolerate inline comments like `local   # local | mcp`
    return value or default


def _int(key: str, default: int) -> int:
    return int(_str(key, str(default)))


def _float(key: str, default: float, low: float, high: float) -> float:
    """A float within [low, high]; anything unparseable or out of range falls back to the default."""
    try:
        value = float(_str(key, str(default)))
    except ValueError:
        return default
    return value if low <= value <= high else default


def _path(key: str, default: str) -> Path:
    p = Path(_str(key, default))
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


DATA_DIR: Path = _path("DL_DATA_DIR", "../data")
MAX_SPEND_USD: float = float(_str("DL_MAX_SPEND_USD", "20"))   # per UTC day, checked before each case
USAGE_FILE: Path = _path("DL_USAGE_FILE", ".usage.json")

MODEL_STRONG: str = _str("DL_MODEL_STRONG", "claude-sonnet-5-5")
MODEL_FAST: str = _str("DL_MODEL_FAST", "claude-haiku-4-5-20251001")
MODEL_TRIAGE: str = _str("DL_MODEL_TRIAGE", MODEL_FAST)        # triage and translation; defaults to the fast model
# Triage clears a listing only below this score (and only if the model does not flag it). Low on purpose: a real case
# dropped at triage is never seen again, so we over-send. A bad or out-of-range value falls back to this default.
# Agents talk at the same time (dynamic) or take turns one at a time in a fixed order. On for the live demo;
# run_eval turns it off so evaluations are cheaper and repeatable.
DYNAMIC: bool = _str("DL_DYNAMIC", "1").lower() not in ("0", "false", "no", "off")
TRIAGE_THRESHOLD: float = _float("DL_TRIAGE_THRESHOLD", 0.15, 0.0, 1.0)

MAX_INPUT_TOKENS: int = _int("DL_MAX_INPUT_TOKENS", 6_000_000)
CASE_TIMEOUT_S: float = float(_str("DL_CASE_TIMEOUT_S", "420"))   # one case, all rounds; stalled agents fail the case instead of hanging
MAX_OUTPUT_TOKENS: int = _int("DL_MAX_OUTPUT_TOKENS", 600_000)
CASE_MAX_INPUT_TOKENS: int = _int("DL_CASE_MAX_INPUT_TOKENS", 300_000)
CASE_MAX_OUTPUT_TOKENS: int = _int("DL_CASE_MAX_OUTPUT_TOKENS", 30_000)

MARKETPLACE_MODE: str = _str("MARKETPLACE_MODE", "local")          # local | mcp
MARKETPLACE_MCP_URL: str = _str("MARKETPLACE_MCP_URL", "http://localhost:8100/mcp")
TRANSPORT: str = _str("TRANSPORT", "local")                        # local | agntcy
DISCOVERY: str = _str("DISCOVERY", "local")                        # local | agntcy
SLIM_ENDPOINT: str = _str("SLIM_ENDPOINT", "http://localhost:46357")
SLIM_SHARED_SECRET: str = _str("SLIM_SHARED_SECRET", "deadlang-shared-secret-change-me-32chars-min")
DIRECTORY_ADDRESS: str = _str("DIRECTORY_ADDRESS", "127.0.0.1:8888")
AGNTCY_BROADCAST: bool = _str("DL_AGNTCY_BROADCAST", "1").lower() not in ("0", "false", "no")   # 0: one call per agent

# Kong gateway (infra/kong/). When DL_GATEWAY_URL is set, every model call goes to Kong instead of straight to Anthropic,
# with the gateway key of its role (DL_GATEWAY_KEY_TRIAGE, _AGENTS, _EXAMINER, _TRANSLATION, _EVAL; DL_GATEWAY_KEY is
# the fallback). Kong holds the real Anthropic key, so these processes do not need ANTHROPIC_API_KEY. Unset: direct.
GATEWAY_URL: str = _str("DL_GATEWAY_URL", "")
# Run triage in the live queue before the agents (screened_out items skip them). Off: every item goes to the agents.
LIVE_TRIAGE: bool = _str("DL_LIVE_TRIAGE", "0").lower() in ("1", "true", "yes", "on")


def gateway_key(role: str) -> str:
    """The gateway key for a role, read at call time (render.py may have just written it to .env)."""
    return _str(f"DL_GATEWAY_KEY_{role.upper()}", "") or _str("DL_GATEWAY_KEY", "")
