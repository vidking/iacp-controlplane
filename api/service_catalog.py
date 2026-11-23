"""Catálogo de aplicaciones independiente de las Golden Images."""

from hashlib import sha256
import json
import os
from pathlib import Path

APPLICATIONS = {
    "nextcloud": {
        "label": "Nextcloud",
        "components": ["app", "postgres", "redis", "cron", "caddy"],
    },
    "n8n": {"label": "n8n", "components": ["app", "postgres", "caddy"]},
    "hermes": {"label": "Hermes Agent", "components": ["app"]},
    "openclaw": {"label": "OpenClaw", "components": ["app"]},
}

SUPPORTED_SERVICE_IMAGE_IDS = ["ubuntu-2404"]
SUPPORTED_AGENT_IMAGE_IDS = ["ubuntu-2404"]


def supported_image_ids(application_id: str) -> list[str]:
    if application_id in {"nextcloud", "n8n"}:
        return SUPPORTED_SERVICE_IMAGE_IDS
    if application_id in {"hermes", "openclaw"}:
        return SUPPORTED_AGENT_IMAGE_IDS
    return []


def enabled(name: str) -> bool:
    return os.getenv(name, "0").lower() in {"1", "true", "yes"}


def _releases():
    default_path = Path(__file__).resolve().parents[1] / "services/releases.json"
    return json.loads(Path(os.getenv("IACP_SERVICE_RELEASES_FILE", default_path)).read_text())


def load_service_release(application_id: str) -> dict:
    return _releases()[application_id]


def service_release_digest(release: dict) -> str:
    return sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()


def catalog() -> list[dict]:
    releases = _releases()
    return [
        dict(
            application_id=key,
            recipe_version=releases[key]["recipe_version"],
            version=releases[key]["version"],
            validation_scope=releases[key]["validation_scope"],
            image_id="ubuntu-2404",
            supported_image_ids=supported_image_ids(key),
            access_modes=["domain", "ip"] if key in {"nextcloud", "n8n"} else ["ssh"],
            agent_modes=["connected", "local"] if key in {"hermes", "openclaw"} else [],
            enabled=enabled("IACP_SERVICES_ENABLED"),
            **value,
        )
        for key, value in APPLICATIONS.items()
    ]
