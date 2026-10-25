# Watchdog

Wildlife traffickers rarely write "ivory". They write "old bone carving", "mammoth-style", "vintage piano key". Keyword filters miss it.

Watchdog reads flagged marketplace listings the way an investigator would. Specialists check the price, seller, photos and wording. Then three AI agents with opposing goals argue over the evidence, and every claim has to cite a source:

- **Undercover** looks for red flags.
- **Counsel** looks for innocent explanations.
- **Case officer** compares against past cases.

They end on one of four outcomes: likely illegal, contested, watching, or dismissed. Each comes with a case file.

**1st place, Cisco track (When AI Agents Disagree), UCSC AI Hackathon.**

[Demo video](https://youtu.be/Vn_vT54s7Kw)

## This fork: Watchdog behind Kong Gateway

Watchdog was built as a team at the hackathon (commits by snek152, lordavder and SrimanRatnapu in this history). This
fork, by Sriman Ratnapu, adds [Kong Gateway](https://konghq.com/products/kong-gateway) in front of every model call:

- **One gateway for all model traffic.** Triage, the three agents, the photo examiner and translation each get their own
  Kong consumer and key. Kong holds the real Anthropic key and reports tokens per role and model on Prometheus, one
  view of model use across all of Watchdog's processes.
- **Prompt guard as a red flag.** Kong's `ai-prompt-guard` checks the seller's text at triage. A listing that tries to
  instruct the AI is blocked before any model sees it, and Watchdog flags it with score 1.0.
- **Measured through the real gateway.** The guard blocked 13 of 26 red-team attacks (all direct, role-play and buried
  ones; no paraphrased or non-English ones), 0 of 12 benign look-alikes, and 1 of 510 stored listings (the team's own
  injection test).

Details, design choices and limits: [docs/kong.md](docs/kong.md). Quick start: `make kong-mock` (offline, no key), then
`DL_GATEWAY_URL=http://localhost:8010` in `.env`.

## Run it

```
cp .env.example .env     # add ANTHROPIC_API_KEY
cd backend && uv run uvicorn deadlang.api.server:app --port 8000
cd frontend && npm install && npm run dev
```

Open http://localhost:3000. Backend tests: `cd backend && uv run pytest -q -m "not integration"`.
