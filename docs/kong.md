# Watchdog behind Kong Gateway

This fork puts [Kong Gateway](https://konghq.com/products/kong-gateway) between Watchdog and the Anthropic API. Every
model call (triage, the three agents, the photo examiner, translation, evaluations) goes through Kong. Watchdog is the
group project from the UCSC AI Hackathon (credits in the README). The Kong integration in this fork is my own addition.

## Why a gateway

Watchdog had three problems that a gateway solves in one place:

| Problem in Watchdog | What Kong does here |
| --- | --- |
| Spend is tracked per process. In the multi-process setup, agent and examiner calls happen in other processes, so the API process undercounts cost. | Every call passes one gateway. Prometheus metrics give tokens per role and model, from all processes. |
| Every process needs the Anthropic API key. | Watchdog holds only per-role gateway keys. Kong adds the real key, which it reads from its own environment. |
| Seller text that tries to instruct the AI (prompt injection) is only handled inside the prompts. | Kong's prompt guard checks triage calls first. A listing that trips it is flagged with score 1.0 and never cleared. |
| A referral to an investigator has no record of how the AI reached it that can be checked later. | `watchdog-audit`, a Kong plugin written in Go, keeps a hash chain of every model call, by case, so an entry edited later shows up. |

## How it works

```
Watchdog (one gateway key per role)          Kong :8010  POST /v1/messages                     Anthropic
  triage ──────┐                             1. key-auth         which role is calling
  agents ──────┤                             2. watchdog-audit   hash the request (our Go plugin)
  examiner ────┼──── X-Watchdog-Case ──────► 3. rate-limiting    per role                     ──► api.anthropic.com
  translation ─┤                             4. ai-prompt-guard  triage calls only
  eval ────────┘                             5. ai-proxy         preserve mode, adds the real key
                                             log: watchdog-audit appends to the hash chain; prometheus counts tokens
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

## The watchdog-audit plugin (Go)

`infra/kong/plugins/watchdog-audit` is a Kong plugin written with Kong's Go PDK. It runs as an external plugin server
next to Kong; `infra/kong/Dockerfile` builds it, runs its tests and adds it to the `kong/kong-gateway:3.12` image.

For every request it records the role (Kong consumer), the case (the `X-Watchdog-Case` header, which `llm.py` sends
and the plugin removes before Anthropic), the model, the status, whether Kong answered itself (a guard block), and
SHA-256 hashes of the request and response bodies. It never stores the bodies themselves. Each entry includes the hash
of the entry before it, and its own hash covers all of its fields, so an edited, removed or reordered entry breaks the
chain from that point on.

- **Phases.** `access` hashes the request and reads the case; `response` hashes the model's answer; `log` appends the
  entry. `log` runs for every request, so blocked calls are recorded too.
- **Priority 1000.** After `key-auth` (1250), so the role is known, and before `ai-prompt-guard` and `ai-proxy`, so a
  request the guard blocks is still hashed.
- **Restarts.** On start the plugin re-reads and verifies the existing chain and continues from its last hash. It
  refuses to extend a chain that does not verify.
- **Checking it.** `scripts/verify_audit_chain.py` (Python, stdlib only) re-computes every hash and lists the calls for
  a case. The hashing rules are pinned by the same test vector in Go (`chain_test.go`) and Python
  (`backend/tests/test_audit_chain.py`).

```
$ python scripts/verify_audit_chain.py infra/kong/audit/chain.jsonl --case C-0102
verified: 4 entries, chain head 5cfb0838e26aeaf5
calls by role: agents 1, examiner 1, triage 2
blocked by the gateway: 1

case C-0102: 3 calls
  #2     2026-10-25T07:18:50  triage      claude-haiku-4-5-20251001    200  req f1246a84f491  resp ac644d7539b0
  #3     2026-10-25T07:18:50  agents      claude-sonnet-5-5            200  req cf2862113f54  resp 21dafa2ef83d
  #4     2026-10-25T07:18:50  examiner    claude-sonnet-5-5            200  req 0429954c6b57  resp 42e191377c18
```

What it does not do: a hash chain shows that the log was not changed after the fact. It cannot stop someone with
write access from replacing the whole file. Publishing the chain head (for example, with each case file) would cover
that.

## Dashboard (Grafana)

![Watchdog Kong dashboard](img/kong-dashboard.png)

`make kong-dash` adds Prometheus and Grafana to the offline stack. Grafana opens on the dashboard at
http://localhost:3001, with no login needed to view it. `make kong-traffic` sends five minutes of synthetic
Watchdog-shaped calls through Kong. The screenshot above is that traffic against the offline stand-in, not real spend.

| Panel | Built from |
| --- | --- |
| Model calls, tokens, guard blocks | `kong_http_requests_total`, `kong_ai_llm_tokens_total` (prometheus plugin, `ai_metrics` on) |
| Estimated cost, total and by role | tokens × a per-model price series (`observability/prices.rules.yml`, the same prices as `costs.py`, checked by a test), joined `on(ai_model, token_type)` |
| Tokens and requests per minute by role | the `consumer` label: one Kong consumer per Watchdog role |
| Prompt guard: triage calls vs blocks | 400s that Kong produced itself (`source="kong"`) for the triage consumer |
| Model latency p95 by model, Kong's own overhead | `kong_ai_llm_provider_latency_ms`, `kong_kong_latency_ms` |
| Rate-limited requests | 429s from the per-role `rate-limiting` plugin |

## Measured

Two runs, kept apart below: the guard eval used real Kong with an offline stand-in for the Anthropic API
(`infra/kong/mock_anthropic.py`, which answers like the API without a key or cost); the end-to-end check used real Kong
and the real API.

### Guard eval: real Kong 3.12, offline stand-in

`scripts/kong_guard_eval.py` sends every listing through Watchdog's real triage call and the real gateway. Kong points
at the stand-in, so nothing is billed. Results are in `results/kong_guard.json`.

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

### End-to-end check: real Kong 3.12, real Anthropic API

Watchdog's full pipeline ran on 6 stored listings with Kong in front of `api.anthropic.com` (`make kong`, Anthropic key
in Kong's environment only; Watchdog held only the gateway keys). Summary in `results/kong_real.json`.

| Check | Result |
| --- | --- |
| Calls through Kong | 83: 80 from the 6 listings (6 triage, 1 translation, 24 photo examiner, 49 agent turns), 2 from `kong_demo.py`, 1 smoke test |
| Errors | 0. Every call returned 200 except the demo's injection listing, which the guard blocked (a 400 from Kong itself) |
| Tools, images, thinking, prompt caching in `preserve` mode | Passed through unchanged; cache reads on agent and examiner calls |
| Audit chain | `verified: 83 entries`; calls by role agents 49, examiner 24, translation 1, triage 9; an edited copy fails verification |
| Tokens per role | Kong's prompt and completion tokens equal Anthropic's `input_tokens` and `output_tokens` for every role. Kong 3.12 does not count cache tokens (145,184 cache reads and 21,406 cache writes here) |
| Cost | $1.24 billed (the script estimated $0.50, worst case $1.13). Kong's tokens times list prices give $1.16, about 7% low: the missing cache tokens |
| Kong's own overhead | Median 5 ms or less, p95 50 ms or less (Prometheus histogram buckets, real traffic). Model time averaged 8.4 s per agent call, 5.1 s per examiner call, 1.1 s per triage call |

The 6 listings were all true positives (the sampler takes positives first), so this run checks the plumbing, not
accuracy. The estimate was low because these listings needed twice the examiner calls it assumed (24, not 12) and the
agents wrote longer answers than in earlier runs.

## Kong versions after 3.12

This project pins `kong/kong-gateway:3.12`. Checked in the newer images' plugin code:

| Version | Change that matters here |
| --- | --- |
| 3.13 | `ai-proxy`'s `llm_format` accepts `anthropic` |
| 3.14 | Anthropic's `cache_read_input_tokens` and `cache_creation_input_tokens` are added into `prompt_tokens` |
| 3.16 | Latest at the time of writing; `preserve` is still accepted and still deprecated |

So the two gaps above are closed upstream. The next step is to move to 3.14 or later and replace `preserve` with the
native Anthropic format, then re-check that tools, images, thinking and caching still pass and that the audit plugin
still works. That has not been done or tested yet.

## Decisions and trade-offs

| Decision | Why | Cost of it |
| --- | --- | --- |
| `ai-proxy` in `preserve` mode, not `llm/v1/chat` | Kong 3.12's `llm_format` has no Anthropic option (3.13 adds one), and converting to OpenAI's format would lose tools, image blocks, thinking and prompt caching | Kong can't rewrite or route between models. It proxies, authenticates and meters. |
| Prompt guard on the triage consumer only | Agent prompts carry the debate (`stance=dismiss`, quotes of the seller), which would trip the guard on our own words | Agents rely on Watchdog's delimiting. A listing only meets the guard if triage runs (`DL_LIVE_TRIAGE=1` in the live queue). |
| A guard block is a red flag (score 1.0), not an error | An injection attempt is itself evidence. Failing open or dropping the listing would reward it. | One more listing for the agents to review |
| Per-role keys rendered into `kong.yml` | `key-auth` treats a `{vault://...}` reference as the literal key: credentials are not a referenceable field. Only the upstream Anthropic key uses the env vault. | `kong.yml` holds the gateway keys, so it is gitignored |
| Audit plugin in Go (external plugin server) | Unit-testable chain logic without Kong, and a typed language for the hashing | Every request makes RPC calls to the plugin server. Kong's own overhead measured about 8 ms p50 and 48 ms p95 in the synthetic demo traffic against the stand-in, and 5 ms or less p50 on the real run. Reading the response body in the `response` phase makes Kong buffer the whole response, so this route can't stream. A Lua or Wasm version would avoid the RPC. |
| Hash chain in a file, one writer | Simple, verifiable by anyone with Python, survives restarts (it re-verifies and resumes) | One Kong node only. Several nodes would each need their own chain, or a shared store. |
| Patterns fixed before the red-team set | Avoids tuning the guard to the test | The headline is 13 of 26, not a tuned number |

## Run it

```bash
make kong-dash          # offline: renders kong.yml, builds Kong + the Go plugin, starts the stand-in, Prometheus, Grafana
# in .env: DL_GATEWAY_URL=http://localhost:8010, then run Watchdog as usual
make kong-demo          # two-minute guided walkthrough (guard block, tokens by role, audit chain, tamper check)
make kong-traffic       # five minutes of synthetic traffic for the dashboard
make kong-eval          # the guard measurement above
make kong-verify        # check the audit chain
```

With the real API, put `ANTHROPIC_API_KEY` in the environment Kong starts from and run `make kong`. Watchdog itself
then needs only the `DL_GATEWAY_KEY_*` values, which `render.py` writes to `.env`.

Token use per role:

```bash
curl -s 127.0.0.1:8011/metrics | grep 'kong_ai_llm_tokens_total.*total_tokens'
```

## Limits

- On 3.12, Kong's token counts leave out prompt-cache reads and writes (`prompt_tokens` is the uncached input), so a
  cost built from them is low (about 7% on the real run). 3.14 counts them. For exact cost, Watchdog's own usage file
  counts caching.
- Rate limits count requests per role, not tokens.
- The audit chain catches an entry edited without recomputing the hashes after it. It is not signed or published
  anywhere, so someone with write access could rewrite the whole file. "Blocked" is inferred from a 400 that Kong
  produced itself.
- Token-based rate limits (AI Rate Limiting Advanced), semantic caching and the semantic prompt guard need Kong
  Enterprise or Konnect. Everything above runs on the free `kong/kong-gateway` image without a license.
- The guard covers triage. The original app sends every live item straight to the agents; set `DL_LIVE_TRIAGE=1` to
  run triage first in the live queue (`api/server.py` `real_worker`). A confident clear is then `screened_out`, and a
  guard block goes on to the agents with the guard's reason on the queue row.
