#!/usr/bin/env bash
set -Eeuo pipefail
secret_dir=$(mktemp -d)
project="telegram-cloud-smoke-${secret_dir##*.}"
project=${project,,}
export CLOUD_SECRET_DIR="$secret_dir" CLOUD_IMAGE_TAG="$project"
export CLOUD_PUBLIC_BASE_URL=https://pilot.example CLOUD_REDIRECT_URIS=https://client.example/callback
export CLOUD_BIND_IP=127.0.0.1 CLOUD_BIND_PORT=0 CLOUD_TRUSTED_PROXY_IP=127.0.0.1
export TELEGRAM_API_ID=1 CLOUD_UID=10001 CLOUD_GID=10001
compose=(docker compose -p "$project" -f docker-compose.cloud.yml)
cleanup() {
    "${compose[@]}" down --volumes --rmi all
    for name in first-bearer second-bearer routes.json api-hash; do
        if [[ -f "$secret_dir/$name" ]]; then unlink "$secret_dir/$name"; fi
    done
    rmdir "$secret_dir"
}
trap cleanup EXIT
python3 - "$secret_dir" <<'PY'
import hashlib, json, secrets, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
routes = {}
for slot in ('first', 'second'):
    bearer = secrets.token_hex(32)
    (directory / f'{slot}-bearer').write_text(bearer)
    routes[slot] = dict(url=f'http://cloud-{slot}:8000/v1/mcp', bearer=bearer, telegram=True,
                        invite_sha256=hashlib.sha256(secrets.token_bytes(32)).hexdigest(), expires=int(time.time()) + 600)
(directory / 'routes.json').write_text(json.dumps(routes))
(directory / 'api-hash').write_text(secrets.token_hex(16))
PY
chmod 0444 "$secret_dir"/{first-bearer,second-bearer,routes.json,api-hash}
"${compose[@]}" build cloud-oauth
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 100
"${compose[@]}" exec -T cloud-oauth python -c "import httpx; c=httpx.Client(base_url='http://127.0.0.1:8001', headers={'CF-Connecting-IP':'127.0.0.1'}, trust_env=False); assert c.get('/.well-known/oauth-authorization-server').status_code == 200; assert c.post('/mcp').status_code == 401"
