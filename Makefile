UV ?= uv
PYTEST := $(UV) run pytest

.PHONY: setup lint test test-security test-installer security check docker-validate deploy update

setup:
	$(UV) sync --frozen

lint:
	$(UV) run ruff check src scripts tests

test:
	$(PYTEST) -q -m "not integration"

test-security:
	$(PYTEST) -q tests/test_session_acl.py tests/test_session_acl_counts.py \
		tests/test_mcp_tool_acl_integration.py tests/test_mtproto_api_acl.py \
		tests/test_session_token_path_security.py tests/unit/tools/test_url_security.py

test-installer:
	$(PYTEST) -q tests/test_deployment_product.py

security:
	./scripts/secret-scan.sh --worktree --history
	git diff --check

check: lint test test-security test-installer security

docker-validate:
	docker compose --env-file .env.example config --quiet
	docker build --check .

deploy:
	./scripts/deploy.sh

update:
	./scripts/update.sh
