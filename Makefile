.PHONY: up down status kong kong-mock kong-down kong-eval
up:
	./scripts/stack.sh up
down:
	./scripts/stack.sh down
status:
	./scripts/stack.sh status

# Kong gateway in front of the model calls (docs/kong.md)
kong:
	python3 infra/kong/render.py && docker compose -f infra/kong/compose.yaml up -d
kong-mock:
	KONG_UPSTREAM_URL=http://mock-anthropic:9999 python3 infra/kong/render.py && docker compose -f infra/kong/compose.yaml --profile mock up -d
kong-down:
	docker compose -f infra/kong/compose.yaml --profile mock down
kong-eval:
	cd backend && DL_GATEWAY_URL=http://localhost:8010 uv run python ../scripts/kong_guard_eval.py
