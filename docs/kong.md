# Watchdog behind Kong Gateway

This fork puts [Kong Gateway](https://konghq.com/products/kong-gateway) between Watchdog and the Anthropic API. Every
model call (triage, the three agents, the photo examiner, translation, evaluations) goes through Kong. Watchdog is the
group project from the UCSC AI Hackathon (credits in the README). The Kong integration in this fork is my own addition.

## Why a gateway

The project review found three problems a gateway solves in one place:

| Problem in Watchdog | What Kong does here |
| --- | --- |
| Spend is tracked per process. In the multi-process setup, agent and examiner calls happen in other processes, so the API process undercounts cost. | Every call passes one gateway. Prometheus metrics give tokens per role and model, from all processes. |
| Every process needs the Anthropic API key. | Watchdog holds only per-role gateway keys. Kong adds the real key, which it reads from its own environment. |
| Seller text that tries to instruct the AI (prompt injection) is only handled inside the prompts. | Kong's prompt guard checks triage calls first. A listing that trips it is flagged with score 1.0 and never cleared. |

## How it works

```
triage ─┐                      ┌─ key-auth: one consumer per role (triage, agents, examiner, translation, eval)
agents ─┤                      ├─ ai-prompt-guard: triage consumer only
examiner┼─► Kong :8010 /v1/messages ─┼─ rate-limiting per consumer ─────────────► api.anthropic.com
translation                    ├─ ai-proxy (route_type preserve): swaps in the real key, logs token usage
eval ───┘                      └─ prometheus (ai_metrics, per consumer) ─► :8011/metrics
```

- **`ai-proxy` in `preserve` mode.** Watchdog sends native Anthropic requests with tools, images, adaptive thinking and
  prompt caching. Kong 3.12's `llm_format` does not accept Anthropic's format, so `preserve` passes the body through
  unchanged. Kong still reads the response's `usage` block for its token metrics.
- **The guard is scoped to triage.** An agent's prompt includes the debate so far (for example `stance=dismiss`, or a
  claim quoting the seller's text). A guard there would block Watchdog's own words. A triage prompt holds only our facts
  and the seller's text between markers, so a match there points at the seller.
- **A block is a red flag, not an error.** `llm.py` tells a Kong block apart from an Anthropic error: Kong's block has no
  `X-Kong-Upstream-Latency` header and an untyped error body. It raises `GatewayBlocked`, and triage returns score 1.0
  with the reason "Blocked by the Kong prompt guard". The listing goes on to the agents, which keep their own defence:
  seller text is delimited as untrusted data (`untrusted.py`).

## Measured

`scripts/kong_guard_eval.py` sends every listing through Watchdog's real triage call and the real gateway. Kong points
at the offline stand-in, so nothing is billed. Results are in `results/kong_guard.json`.

| Set | Blocked |
| --- | --- |
| Red-team attacks (`data/redteam/injections.jsonl`) | 13 of 26 |
| &nbsp;&nbsp;direct ("ignore all previous instructions") | 4 of 4 |
| &nbsp;&nbsp;role and format (`<system>`, "you are now", "set your score") | 4 of 4 |
| &nbsp;&nbsp;buried in a long description | 2 of 2 |
| &nbsp;&nbsp;addressed to the AI ("note to automated reviewers") | 2 of 4 |
| &nbsp;&nbsp;obfuscated (spaced letters, digits, Cyrillic letters) | 1 of 4 |
| &nbsp;&nbsp;paraphrased, no trigger words | 0 of 4 |
| &nbsp;&nbsp;Japanese, Chinese, Vietnamese, Spanish | 0 of 4 |
| Benign look-alikes ("care instructions", "AI robot figure", "note to buyers") | 0 of 12 |
| All stored listings (479 real eBay + reconstructed cases + test listings) | 1 of 510: L9001, the team's injection test |

How to read it:

- The patterns were written and frozen before the red-team set, and were not changed after measuring. The set is small
  and self-written, so these numbers are indicative only.
- A regex guard is a cheap first filter. It catches the blunt attacks at no model cost and false-blocked nothing here.
  It misses paraphrases and other languages. Kong's AI Semantic Prompt Guard (Enterprise) matches by meaning and is
  the natural next step for those. The agents' delimiting stays the main defence.

## Run it

```bash
python infra/kong/render.py                      # writes infra/kong/kong.yml; adds DL_GATEWAY_KEY_* to .env if missing
docker compose -f infra/kong/compose.yaml up -d  # Kong on :8010 (proxy) and 127.0.0.1:8011 (admin, /metrics)
# then in .env: DL_GATEWAY_URL=http://localhost:8010   (ANTHROPIC_API_KEY is now needed only by Kong)
```

Offline, with no key and no spend:

```bash
KONG_UPSTREAM_URL=http://mock-anthropic:9999 python infra/kong/render.py
docker compose -f infra/kong/compose.yaml --profile mock up -d
```

Token use per role:

```bash
curl -s 127.0.0.1:8011/metrics | grep 'kong_ai_llm_tokens_total.*total_tokens'
```

## Limits

- Kong's token counts leave out prompt-cache reads and writes (`prompt_tokens` is the uncached input). For exact cost,
  Watchdog's own usage file still counts caching.
- Token-based rate limits (AI Rate Limiting Advanced), semantic caching and the semantic prompt guard need Kong
  Enterprise or Konnect. Everything above runs on the free `kong/kong-gateway` image without a license.
- The guard covers triage. The original app sends every live item straight to the agents; set `DL_LIVE_TRIAGE=1` to
  run triage first in the live queue (`api/server.py` `real_worker`). A confident clear is then `screened_out`, and a
  guard block goes on to the agents with the guard's reason on the queue row.
