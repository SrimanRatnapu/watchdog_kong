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
| A referral to an investigator has no record of how the AI reached it that can be checked later. | `watchdog-audit`, a Kong plugin written in Go, keeps a tamper-evident hash chain of every model call, by case. |

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
make kong-mock          # offline: renders kong.yml, builds Kong + the Go plugin, starts the Anthropic stand-in
# in .env: DL_GATEWAY_URL=http://localhost:8010, then run Watchdog as usual
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

- Kong's token counts leave out prompt-cache reads and writes (`prompt_tokens` is the uncached input). For exact cost,
  Watchdog's own usage file still counts caching.
- Token-based rate limits (AI Rate Limiting Advanced), semantic caching and the semantic prompt guard need Kong
  Enterprise or Konnect. Everything above runs on the free `kong/kong-gateway` image without a license.
- The guard covers triage. The original app sends every live item straight to the agents; set `DL_LIVE_TRIAGE=1` to
  run triage first in the live queue (`api/server.py` `real_worker`). A confident clear is then `screened_out`, and a
  guard block goes on to the agents with the guard's reason on the queue row.
