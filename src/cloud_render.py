"""Generate the community Compose file and its persistent per-slot routes."""

import json
import os
import secrets
import sys
from pathlib import Path

import yaml

from src.cloud_slots import slot_count


def render(target, secret_dir):
    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.cloud.yml").read_text())
    services = compose["services"]
    backend = services["cloud-first"]
    oauth = services["cloud-oauth"]
    oauth["build"]["context"] = str(root)
    oauth["environment"].pop("MCP_OAUTH_REDIRECT_URIS")
    oauth["environment"].update(CLOUD_BOT_USERNAME="${CLOUD_BOT_USERNAME:?set bot username}",
                                CLOUD_BOT_TOKEN_FILE="/run/secrets/bot_token", CLOUD_SLOTS=str(slot_count()))
    oauth["environment"]["CLOUD_HUMAN_URL"] = "${CLOUD_HUMAN_URL:-}"
    oauth["environment"]["CLOUD_HUMAN_PROXY_IP"] = "${CLOUD_HUMAN_PROXY_IP:-}"
    oauth["secrets"].append("bot_token")
    oauth["volumes"] = ["community-oauth:/data/oauth"]
    oauth["networks"] = ["origin"]
    services.clear()
    services["cloud-oauth"] = oauth
    services["cloud-bot"] = {**compose["x-common"], "command": ["python", "-m", "src.cloud_bot"],
                             "volumes": oauth["volumes"], "networks": ["origin"], "secrets": ["bot_token"],
                             "healthcheck": {"disable": True}, "environment": {
                                 **{k: oauth["environment"][k] for k in ("PUBLIC_BASE_URL", "OAUTH_DB_PATH", "CLOUD_SLOTS",
                                                                       "CLOUD_BOT_USERNAME", "CLOUD_BOT_TOKEN_FILE", "CLOUD_HUMAN_URL")},
                                 "OWNER_TELEGRAM_ID": "${OWNER_TELEGRAM_ID:?set bot owner}"}}
    compose["secrets"] = {"cloud_routes": {"file": str(secret_dir / "community-routes.json")},
                          "bot_token": {"file": str(secret_dir / "bot-token")},
                          "telegram_api_hash": {"file": str(secret_dir / "api-hash")}}
    compose["networks"] = {"origin": None}
    compose["volumes"] = {"community-oauth": None}
    uid, gid = int(os.getenv("CLOUD_UID", "10001")), int(os.getenv("CLOUD_GID", "10001"))
    os.umask(0o077)
    routes = {}
    for slot in range(1, slot_count() + 1):
        name, private, egress = f"cloud-slot-{slot}", f"slot-{slot}-private", f"slot-{slot}-egress"
        bearer_file = secret_dir / (name + "-bearer")
        if not bearer_file.exists():
            bearer_file.write_text(secrets.token_urlsafe(32))
        os.chown(bearer_file, uid, gid)
        routes[str(slot)] = {"url": f"http://{name}:8000/v1/mcp", "bearer": bearer_file.read_text()}
        compose["secrets"][name] = {"file": str(bearer_file)}
        compose["networks"].update({private: {"internal": True}, egress: None})
        compose["volumes"][name] = None
        oauth["networks"].append(private)
        services[name] = {**backend, "environment": {**backend["environment"], "CLOUD_SLOT": str(slot),
                          "CLOUD_SESSION_PATH": "/data/sessions/account.aes"}, "networks": [private, egress],
                          "volumes": [name + ":/data/sessions"],
                          "secrets": [{"source": name, "target": "backend_bearer"}, "telegram_api_hash"]}
    routes_path = secret_dir / "community-routes.json"
    routes_path.write_text(json.dumps(routes))
    os.chown(routes_path, uid, gid)
    target.write_text(json.dumps(compose, indent=2))


if __name__ == "__main__":
    render(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(strict=True))
