"""The only path to the Anthropic API (contract: plan 01 §4.5): JSON via a tool call, and a hard spend cap.

Request shape depends on the model. Older models (Haiku 4.5, the 4.x family) get the plan's forced
tool_choice and `temperature`. Newer ones (Sonnet 5.5, Opus 5.5, Fable 5.1, Haiku 5.5) return a 400
for forced tool_choice and for non-default temperature, so they get tool_choice "auto" plus a system
line naming the tool, and no temperature. Sonnet 5.5 also gets thinking "between_tools" (its lowest
setting) so thinking does not eat the small max_tokens. Others get effort "low".

Because "auto" does not guarantee a tool call, a reply without one gets exactly one follow-up asking for it.

Spend caps are enforced with reservations: before a call, its estimated input and its max_tokens are added
to an in-memory pending total (checked together with the usage file), so calls running at the same time
cannot together overshoot a cap. The reservation is replaced by actual usage when the call ends, however it ends.
The pending total is per process; the usage file is what processes share.
"""
from __future__ import annotations

import os
import asyncio
import base64
import io
import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from PIL import Image

from deadlang import config, fileio

log = logging.getLogger("deadlang.llm")

MAX_IMAGE_SIDE = 1568
TOKENS_PER_IMAGE = 1600
# The API adds a hidden tool-use system prompt (about 300-350 tokens) and formats the tool definition on every tool
# call, none of which is in our prompt text. Measured on real examiner calls: 1,452 (Opus 5.5) and ~1,500 (two earlier
# 80-call runs) input tokens per call, against 915 counted from prompt, schema and photo.
TOOL_USE_OVERHEAD_TOKENS = 450
# Prompt caching bills a 5-minute cache write at 1.25x the input price and a read at 0.1x.
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.1
# Smallest prefix (tools + system) the API will cache, by model-name prefix; the longest match wins. Shorter prefixes
# are silently not cached. Haiku is never cached here. Anything not listed needs 1,024.
_CACHE_MIN_TOKENS = (("claude-opus-4-5", 4096), ("claude-opus-4-6", 4096), ("claude-opus-4-7", 2048),
                     ("claude-opus-4", 1024), ("claude-sonnet-4", 1024), ("claude-sonnet-5", 1024),
                     ("claude-sonnet-5-5", 512), ("claude-opus-5", 512), ("claude-fable", 512), ("claude-mythos", 512))
CHARS_PER_TOKEN = 3.5
RETRY_STATUSES = {429, 500, 529}
RETRY_SLEEP = 3.0
_IMAGE_CACHE_SIZE = 16

_LEGACY_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4", "claude-opus-4", "claude-3")

_client: Any = None
_lock_for: tuple[Any, asyncio.Lock] | None = None     # (event loop, its lock); a lock belongs to one loop
_pending: dict[str, Any] = {"in": 0, "out": 0, "cases": {}}    # tokens reserved by calls in flight


# Set by the API to persist every call (model, schema_name, case_id, tokens_in, tokens_out, ms).
usage_hook: Any = None
# Like usage_hook but with the cache split, for tools that price each call exactly (the full-set eval):
# (model, schema_name, case_id, tokens_in, tokens_out, cache_read, cache_write, ms). tokens_in is the uncached input.
usage_detail_hook: Any = None


class BudgetExceeded(Exception):
    """A global or per-case token cap would be exceeded by this call."""


def set_client_for_tests(fake: Any) -> None:
    global _client
    _client = fake


# ---------------- Kong gateway ----------------

# Which gateway consumer a call belongs to, by its schema. Kong counts tokens and applies its plugins per consumer.
GATEWAY_ROLES = {"report_screening": "triage", "post_position": "agents", "report_material": "examiner",
                 "report_translation": "translation", "report_decision": "eval"}
_gateway_clients: dict[str, Any] = {}
CASE_HEADER = "X-Watchdog-Case"          # read and removed by the watchdog-audit plugin (infra/kong/plugins/)


class GatewayBlocked(Exception):
    """Kong refused the request itself (its prompt guard), so it never reached the model. Not retried."""

    def __init__(self, role: str):
        self.role = role
        super().__init__(f"Kong prompt guard blocked this {role} call: the listing text gives instructions to the AI")


def gateway_role(schema_name: str) -> str:
    return GATEWAY_ROLES.get(schema_name, "agents")


def _gateway_blocked(exc: Exception) -> bool:
    """A 400 that Kong produced itself: no upstream latency header (the request never left Kong) and Kong's own error
    body, not Anthropic's typed one. Anthropic's 400s pass through Kong with X-Kong-Upstream-Latency set."""
    if not (config.GATEWAY_URL and isinstance(exc, anthropic.BadRequestError)):
        return False
    headers = exc.response.headers
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    return "x-kong-upstream-latency" not in headers and "x-kong-request-id" in headers and "type" not in err


def _get_client(role: str = "agents") -> Any:
    global _client
    if _client is not None:                     # set_client_for_tests, or the direct client below
        return _client
    # A key that is not scoped to one workspace needs the workspace named on every request.
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": ws} if ws else None
    if config.GATEWAY_URL:
        if role not in _gateway_clients:
            key = config.gateway_key(role)
            if not key:
                raise RuntimeError(f"DL_GATEWAY_URL is set but there is no gateway key for '{role}' "
                                   f"(set DL_GATEWAY_KEY_{role.upper()} or DL_GATEWAY_KEY; infra/kong/render.py makes them)")
            _gateway_clients[role] = anthropic.AsyncAnthropic(api_key=key, base_url=config.GATEWAY_URL, max_retries=0,
                                                              timeout=120.0, default_headers=headers)
        return _gateway_clients[role]
    _client = anthropic.AsyncAnthropic(max_retries=0, timeout=120.0,   # retries are ours: at most 1
                                       default_headers=headers)
    return _client


def _get_lock() -> asyncio.Lock:
    global _lock_for
    loop = asyncio.get_running_loop()
    if _lock_for is None or _lock_for[0] is not loop:
        _lock_for = (loop, asyncio.Lock())
    return _lock_for[1]


# ---------------- usage file ----------------

def _empty() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def effective_input(row: dict) -> int:
    """Input tokens as the bill counts them: what the API reports as input, plus cache writes at 1.25x and cache reads
    at 0.1x. Rows only have the cache keys once a call used the cache, so older rows work unchanged. The caps count
    this, because they guard spend."""
    return round(row.get("input_tokens", 0) + row.get("cache_write_tokens", 0) * CACHE_WRITE_FACTOR
                 + row.get("cache_read_tokens", 0) * CACHE_READ_FACTOR)


def _load_usage() -> dict:
    try:
        data = json.loads(Path(config.USAGE_FILE).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("global", _empty())
    data.setdefault("cases", {})
    return data


def _write_usage(data: dict) -> None:
    fileio.write_json_atomic(config.USAGE_FILE, data)


def usage_snapshot(case_id: str | None = None) -> dict:
    data = _load_usage()
    if case_id is None:
        return dict(data["global"])
    return dict(data["cases"].get(case_id, _empty()))


# ---------------- budget: check, reserve, record ----------------

@dataclass
class _Reservation:
    case_id: str | None
    tokens_in: int
    tokens_out: int
    active: bool = True


def _check_budget(case_id: str | None, est_in: int, max_out: int) -> None:
    """Raise BudgetExceeded if this call, on top of usage so far and calls in flight, would pass a cap."""
    data = _load_usage()
    g = data["global"]
    if effective_input(g) + _pending["in"] + est_in > config.MAX_INPUT_TOKENS:
        raise BudgetExceeded(_over("global", "input", config.MAX_INPUT_TOKENS, effective_input(g), _pending["in"], est_in))
    if g["output_tokens"] + _pending["out"] + max_out > config.MAX_OUTPUT_TOKENS:
        raise BudgetExceeded(_over("global", "output", config.MAX_OUTPUT_TOKENS, g["output_tokens"], _pending["out"], max_out))
    if case_id is None:
        return
    c = data["cases"].get(case_id, _empty())
    p_in, p_out = _pending["cases"].get(case_id, (0, 0))
    if effective_input(c) + p_in + est_in > config.CASE_MAX_INPUT_TOKENS:
        raise BudgetExceeded(_over(f"case {case_id}", "input", config.CASE_MAX_INPUT_TOKENS, effective_input(c), p_in, est_in))
    if c["output_tokens"] + p_out + max_out > config.CASE_MAX_OUTPUT_TOKENS:
        raise BudgetExceeded(_over(f"case {case_id}", "output", config.CASE_MAX_OUTPUT_TOKENS, c["output_tokens"], p_out, max_out))


def _over(scope: str, kind: str, cap: int, used: int, in_flight: int, need: int) -> str:
    """Say which cap, and the sum that did not fit. `need` is max_tokens for output: the cap reserves what a
    call COULD write, then reconciles to what it did write."""
    return f"{scope} {kind} cap {cap:,}: used {used:,} + {in_flight:,} in flight + {need:,} for this call > {cap:,}"


async def _reserve(case_id: str | None, est_in: int, max_out: int) -> _Reservation:
    async with _get_lock():
        _check_budget(case_id, est_in, max_out)
        res = _Reservation(case_id, est_in, max_out)
        _pending["in"] += est_in
        _pending["out"] += max_out
        if case_id:
            p_in, p_out = _pending["cases"].get(case_id, (0, 0))
            _pending["cases"][case_id] = (p_in + est_in, p_out + max_out)
    return res


def _release(res: _Reservation) -> None:
    """Give a reservation back. Synchronous and idempotent, so it is safe in a finally, even when cancelled."""
    if not res.active:
        return
    res.active = False
    _pending["in"] -= res.tokens_in
    _pending["out"] -= res.tokens_out
    if res.case_id:
        p_in, p_out = _pending["cases"].get(res.case_id, (0, 0))
        left = (p_in - res.tokens_in, p_out - res.tokens_out)
        if left == (0, 0):
            _pending["cases"].pop(res.case_id, None)
        else:
            _pending["cases"][res.case_id] = left


def _record_now(res: _Reservation, tokens_in: int, tokens_out: int, cache_read: int = 0, cache_write: int = 0) -> None:
    """Add usage to the file and release the reservation. Never awaits, so nothing can interleave with it.
    `tokens_in` is the uncached input the API reports; cached tokens go in their own keys, written only when non-zero."""
    data = _load_usage()   # re-read every time so other processes' usage is kept
    rows = [data["global"]] + ([data["cases"].setdefault(res.case_id, _empty())] if res.case_id else [])
    for row in rows:
        row["input_tokens"] += tokens_in
        row["output_tokens"] += tokens_out
        row["calls"] += 1
        if cache_read:
            row["cache_read_tokens"] = row.get("cache_read_tokens", 0) + cache_read
        if cache_write:
            row["cache_write_tokens"] = row.get("cache_write_tokens", 0) + cache_write
    _write_usage(data)
    _release(res)


async def _record(res: _Reservation, tokens_in: int, tokens_out: int, cache_read: int = 0, cache_write: int = 0) -> None:
    async with _get_lock():
        _record_now(res, tokens_in, tokens_out, cache_read, cache_write)


# ---------------- request building ----------------

def thinks(model: str) -> bool:
    """Does this model think (or write progress text) before answering? Those tokens are output tokens and
    count toward max_tokens. The older 4.x models do not."""
    return not _is_legacy(model)


def estimate_image_tokens(path: Path | str) -> int:
    """About width x height / 750 tokens for a photo, after the downscale call_vision_json applies. Unreadable
    file: the flat TOKENS_PER_IMAGE (the conservative figure the budget check uses)."""
    try:
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return TOKENS_PER_IMAGE
    scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
    return max(1, math.ceil(w * scale * h * scale / 750))


def estimate_input_tokens(system: str, user: str, images: int = 0, schema: dict | None = None,
                          image_tokens: int | None = None) -> int:
    """The estimate the budget check uses before every call (also used by the cost reports). The budget check
    keeps the flat, conservative TOKENS_PER_IMAGE; reports can pass a measured `image_tokens`."""
    total = int(len(system + user) / CHARS_PER_TOKEN) + (image_tokens or TOKENS_PER_IMAGE) * images
    if schema is not None:
        total += int(len(json.dumps(schema)) / CHARS_PER_TOKEN)      # the tool definition is sent as input too
    return total


def _is_legacy(model: str) -> bool:
    return model.startswith(_LEGACY_PREFIXES)


def cache_min_tokens(model: str) -> int | None:
    """Smallest prefix this model will cache, or None when we never cache it (Haiku)."""
    if "haiku" in model:
        return None
    matches = [(prefix, n) for prefix, n in _CACHE_MIN_TOKENS if model.startswith(prefix)]
    return max(matches, key=lambda m: len(m[0]))[1] if matches else 1024


def _full_system(model: str, system: str, schema_name: str) -> str:
    if _is_legacy(model):
        return system
    return f"{system}\n\nReturn your answer by calling the `{schema_name}` tool exactly once."


def cached_prefix_tokens(model: str, system: str, schema: dict, schema_name: str) -> int:
    """Tokens of tools + system that will be cached for this request, or 0 if it is too short, or Haiku."""
    minimum = cache_min_tokens(model)
    prefix = estimate_input_tokens(_full_system(model, system, schema_name), "", schema=schema) + TOOL_USE_OVERHEAD_TOKENS
    return prefix if minimum is not None and prefix >= minimum else 0


def _request(model: str, system: str, content: Any, schema_name: str, schema: dict,
             max_tokens: int, temperature: float) -> dict:
    tool = {"name": schema_name, "description": "Return your answer by calling this tool.",
            "input_schema": schema}
    req: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "tools": [tool],
                           "messages": [{"role": "user", "content": content}]}
    full = _full_system(model, system, schema_name)
    # Tools render before system, so one breakpoint at the end of the system block caches both. The block is the same
    # for every call with this prompt (the seller's text and the random marker live in the user message), so it can hit.
    if cached_prefix_tokens(model, system, schema, schema_name):
        req["system"] = [{"type": "text", "text": full, "cache_control": {"type": "ephemeral"}}]
    else:
        req["system"] = full
    if _is_legacy(model):
        req["tool_choice"] = {"type": "tool", "name": schema_name}   # anthropic SDK 1.x has no temperature argument
        return req
    req["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    if model.startswith("claude-sonnet-5-5"):
        req["thinking"] = {"type": "between_tools"}
    else:
        req["output_config"] = {"effort": "low"}
    return req


# ---------------- images: encode off the event loop, once per file version ----------------

_image_cache: OrderedDict[tuple[str, int], dict] = OrderedDict()       # (path, mtime_ns) -> image block, LRU
_image_inflight: dict[tuple[str, int], asyncio.Future] = {}            # encodes already running, shared


def _encode_image(path: Path) -> dict:
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


async def _encoded(path: Path) -> dict:
    key = (str(path), path.stat().st_mtime_ns)      # a changed file is a new key; a missing one raises here
    hit = _image_cache.get(key)
    if hit is not None:
        _image_cache.move_to_end(key)
        return hit
    task = _image_inflight.get(key)
    if task is None:                                # two calls for the same photo at once share one encode
        task = asyncio.ensure_future(asyncio.to_thread(_encode_image, path))
        _image_inflight[key] = task
        task.add_done_callback(lambda _t: _image_inflight.pop(key, None))
    block = await asyncio.shield(task)              # one caller being cancelled must not cancel the others
    _image_cache[key] = block
    _image_cache.move_to_end(key)
    while len(_image_cache) > _IMAGE_CACHE_SIZE:
        _image_cache.popitem(last=False)
    return block


# ---------------- the call ----------------

def _retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):   # includes APITimeoutError
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in RETRY_STATUSES


# A long unattended run can ride out a network blip: while this is above 0, a connection error or timeout is retried
# with growing waits (CONNECTION_BACKOFF = first wait, longest wait) until this many seconds have passed since the
# first failure. Off (0) by default, so the server and the demo keep the single quick retry.
CONNECTION_RETRY_SECONDS = 0.0
CONNECTION_BACKOFF = (2.0, 30.0)


async def _create(req: dict, schema_name: str, case_id: str | None = None) -> Any:
    client = _get_client(gateway_role(schema_name))
    if config.GATEWAY_URL and case_id:
        # Kong's watchdog-audit plugin files the call under this case, then removes the header before Anthropic.
        req = {**req, "extra_headers": {CASE_HEADER: case_id}}
    try:
        return await client.messages.create(**req)
    except Exception as exc:
        if _gateway_blocked(exc):
            raise GatewayBlocked(gateway_role(schema_name)) from exc
        if not _retryable(exc):
            raise
        log.warning("llm retry after %s (%s, %s)", type(exc).__name__, req["model"], schema_name)
        await asyncio.sleep(RETRY_SLEEP)
    deadline = time.monotonic() + CONNECTION_RETRY_SECONDS
    wait = CONNECTION_BACKOFF[0]
    while True:
        try:
            return await client.messages.create(**req)
        except anthropic.APIConnectionError as exc:       # includes APITimeoutError
            if time.monotonic() + wait > deadline:
                raise
            log.warning("llm: %s again; waiting %.0fs (%s, %s)", type(exc).__name__, wait, req["model"], schema_name)
            await asyncio.sleep(wait)
            wait = min(wait * 2, CONNECTION_BACKOFF[1])


def _usage_of(resp: Any) -> tuple[int, int, int, int]:
    """(uncached input, output, cache read, cache write) tokens of a response; cache fields may be missing or None."""
    u = resp.usage
    return (u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0)


async def _attempt(req: dict, schema_name: str, case_id: str | None, est_in: int) -> tuple[dict | None, Any]:
    """One request, budget-reserved and recorded. Returns (tool input or None, the response)."""
    res = await _reserve(case_id, est_in, req["max_tokens"])
    resp = None
    try:
        start = time.perf_counter()
        resp = await _create(req, schema_name, case_id)
        ms = (time.perf_counter() - start) * 1000
        tokens_in, tokens_out, cache_read, cache_write = _usage_of(resp)
        await _record(res, tokens_in, tokens_out, cache_read, cache_write)
    except (asyncio.CancelledError, anthropic.APITimeoutError):
        # The request was (or may have been) sent and then cut off, e.g. by the transport's timeout. It may
        # still be billed, so count it as spent rather than letting it vanish: the real usage if the response
        # had already arrived, otherwise the reserved estimate (conservative).
        if res.active:
            spent = _usage_of(resp) if resp is not None else (res.tokens_in, res.tokens_out, 0, 0)
            try:
                _record_now(res, *spent)
            except OSError:
                log.exception("llm: could not record usage for a cut-off call")
        raise
    finally:
        _release(res)                                   # no-op once recorded; covers every other error
    billed_in = effective_input({"input_tokens": tokens_in, "cache_read_tokens": cache_read, "cache_write_tokens": cache_write})
    log.info("llm model=%s schema=%s case=%s in=%d cache_read=%d cache_write=%d out=%d ms=%.0f",
             req["model"], schema_name, case_id, tokens_in, cache_read, cache_write, tokens_out, ms)
    if usage_hook:
        # cost-equivalent input, so a hook that prices tokens at the input rate (runs.usd) is right with caching on
        usage_hook(req["model"], schema_name, case_id, billed_in, tokens_out, ms)
    if usage_detail_hook:
        usage_detail_hook(req["model"], schema_name, case_id, tokens_in, tokens_out, cache_read, cache_write, ms)
    for block in resp.content:
        if block.type == "tool_use" and block.name == schema_name:
            return _unstringify(dict(block.input)), resp
    return None, resp


def _unstringify(out: dict) -> dict:
    """The model sometimes returns a nested object/array as a JSON string; decode those."""
    for k, v in out.items():
        if isinstance(v, str) and v.lstrip()[:1] in ("{", "["):
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    return out


async def _call(req: dict, schema_name: str, case_id: str | None, est_in: int) -> dict:
    out, resp = await _attempt(req, schema_name, case_id, est_in)
    if out is not None:
        return out
    if getattr(resp, "stop_reason", None) == "refusal":
        raise ValueError("no tool output (the model declined to answer)")    # asking again would just be refused again
    # The model answered in words instead of calling the tool (possible when tool_choice is "auto").
    # Ask once more, with its reply in the conversation; this call is budget-checked and recorded too.
    nudge = f"Call the {schema_name} tool now with your answer."
    log.warning("llm: no %s tool call from %s; asking once more", schema_name, req["model"])
    echoed = [b for b in resp.content if b.type != "tool_use"]
    messages = list(req["messages"]) + ([{"role": "assistant", "content": echoed}] if echoed else [])
    messages.append({"role": "user", "content": nudge})
    est_again = est_in + resp.usage.output_tokens + int(len(nudge) / CHARS_PER_TOKEN)
    out, _ = await _attempt({**req, "messages": messages}, schema_name, case_id, est_again)
    if out is None:
        raise ValueError("no tool output")
    return out


async def call_json(*, model: str, system: str, user: str, schema_name: str,
                    schema: dict, case_id: str | None, max_tokens: int = 2000,
                    temperature: float = 0.0) -> dict:
    req = _request(model, system, user, schema_name, schema, max_tokens, temperature)
    est_in = estimate_input_tokens(system, user, schema=schema)
    return await _call(req, schema_name, case_id, est_in)


async def call_vision_json(*, model: str, system: str, user: str, image_paths: list[Path],
                           schema_name: str, schema: dict, case_id: str | None,
                           max_tokens: int = 1500, temperature: float = 0.0) -> dict:
    blocks = await asyncio.gather(*(_encoded(Path(p)) for p in image_paths))
    content = [*blocks, {"type": "text", "text": user}]
    req = _request(model, system, content, schema_name, schema, max_tokens, temperature)
    est_in = estimate_input_tokens(system, user, images=len(image_paths), schema=schema)
    return await _call(req, schema_name, case_id, est_in)
