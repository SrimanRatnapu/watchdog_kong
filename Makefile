.PHONY: up down status kong kong-mock kong-dash kong-down kong-eval kong-verify kong-traffic kong-demo
up:
	./scripts/stack.sh up
down:
	./scripts/stack.sh down
status:
	./scripts/stack.sh status

# Kong gateway in front of the model calls (docs/kong.md)
kong:
	python3 infra/kong/render.py && mkdir -p infra/kong/audit && chmod 777 infra/kong/audit && docker compose -f infra/kong/compose.yaml up -d --build
kong-mock:
	KONG_UPSTREAM_URL=http://mock-anthropic:9999 python3 infra/kong/render.py && mkdir -p infra/kong/audit && chmod 777 infra/kong/audit && docker compose -f infra/kong/compose.yaml --profile mock up -d --build
kong-dash:
	KONG_UPSTREAM_URL=http://mock-anthropic:9999 python3 infra/kong/render.py && mkdir -p infra/kong/audit && chmod 777 infra/kong/audit && docker compose -f infra/kong/compose.yaml --profile mock --profile observability up -d --build
kong-down:
	docker compose -f infra/kong/compose.yaml --profile mock --profile observability down
kong-eval:
	cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_guard_eval.py
kong-verify:
	python3 scripts/verify_audit_chain.py infra/kong/audit/chain.jsonl
kong-traffic:
	cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_demo_traffic.py --minutes 5
kong-demo:
	cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_demo.py --step
