"""Renderizado determinista. Las imágenes deben venir de un manifiesto revisado."""

import json
import re


class RecipeError(RuntimeError):
    pass


def render(application, configuration, secrets, release):
    expected = {
        "nextcloud": {"app", "postgres", "redis", "caddy"},
        "n8n": {"app", "postgres", "caddy"},
        "hermes": {"app"},
        "openclaw": {"app"},
    }[application]
    images = release.get("images", {})
    if set(images) != expected or not all(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}", v)
        for v in images.values()
    ):
        raise RecipeError("SERVICE_RELEASE_NOT_PINNED")
    app = {"image": images["app"], "restart": "unless-stopped"}
    result = {"services": {"app": app}, "volumes": {"app_data": {}}}
    files = {}

    def env(values):
        # Compose interpola incluso los valores serializados como JSON.
        return {key: str(value).replace("$", "$$") for key, value in values.items()}

    if application in {"nextcloud", "n8n"}:
        db = {
            "image": images["postgres"],
            "restart": "unless-stopped",
            "environment": env(
                {
                    "POSTGRES_DB": "application",
                    "POSTGRES_USER": "application",
                    "POSTGRES_PASSWORD": secrets["database_password"],
                }
            ),
            "volumes": ["database:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U application -d application"],
                "interval": "5s",
                "timeout": "5s",
                "retries": 30,
            },
        }
        result["services"]["postgres"] = db
        result["volumes"]["database"] = {}
        app["depends_on"] = {"postgres": {"condition": "service_healthy"}}
        domain = configuration["domain"]
        port = 80 if application == "nextcloud" else 5678
        result["services"]["caddy"] = {
            "image": images["caddy"],
            "restart": "unless-stopped",
            "ports": ["80:80", "443:443"],
            "volumes": [
                "./Caddyfile:/etc/caddy/Caddyfile:ro,Z",
                "caddy_data:/data",
                "caddy_config:/config",
            ],
        }
        result["volumes"].update(caddy_data={}, caddy_config={})
        tls = (
            " tls /etc/caddy/pilot.crt /etc/caddy/pilot.key\n"
            if configuration.get("access_mode") == "ip"
            else ""
        )
        if tls:
            result["services"]["caddy"]["volumes"] += [
                "./pilot.crt:/etc/caddy/pilot.crt:ro,Z",
                "./pilot.key:/etc/caddy/pilot.key:ro,Z",
            ]
        files["Caddyfile"] = (
            f"https://{domain} {{\n{tls} reverse_proxy app:{port}\n}}\n"
        )
    if application == "nextcloud":
        app["volumes"] = ["app_data:/var/www/html"]
        app["environment"] = env(
            {
                "POSTGRES_HOST": "postgres",
                "POSTGRES_DB": "application",
                "POSTGRES_USER": "application",
                "POSTGRES_PASSWORD": secrets["database_password"],
                "NEXTCLOUD_ADMIN_USER": configuration["admin_user"],
                "NEXTCLOUD_ADMIN_PASSWORD": secrets["admin_password"],
                "NEXTCLOUD_TRUSTED_DOMAINS": configuration["domain"],
                "OVERWRITEPROTOCOL": "https",
                "REDIS_HOST": "redis",
            }
        )
        result["services"]["redis"] = {
            "image": images["redis"],
            "restart": "unless-stopped",
        }
        result["services"]["cron"] = {
            "image": images["app"],
            "restart": "unless-stopped",
            "entrypoint": "/cron.sh",
            "volumes": ["app_data:/var/www/html"],
            "environment": app["environment"],
            "depends_on": ["app"],
        }
    elif application == "n8n":
        app["volumes"] = ["app_data:/home/node/.n8n"]
        app["healthcheck"] = {
            "test": [
                "CMD",
                "node",
                "-e",
                "fetch('http://127.0.0.1:5678/healthz/readiness').then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))",
            ],
            "interval": "10s",
            "timeout": "5s",
            "retries": 30,
            "start_period": "30s",
        }
        app["environment"] = env(
            {
                "DB_TYPE": "postgresdb",
                "DB_POSTGRESDB_HOST": "postgres",
                "DB_POSTGRESDB_DATABASE": "application",
                "DB_POSTGRESDB_USER": "application",
                "DB_POSTGRESDB_PASSWORD": secrets["database_password"],
                "N8N_ENCRYPTION_KEY": secrets["encryption_key"],
                "N8N_HOST": configuration["domain"],
                "N8N_PROTOCOL": "https",
                "WEBHOOK_URL": f"https://{configuration['domain']}/",
                "N8N_EDITOR_BASE_URL": f"https://{configuration['domain']}/",
                "N8N_PROXY_HOPS": "1",
            }
        )
        # Proteger el alta de propietario hasta que el cliente administre Caddy.
        # Caddy requiere bcrypt; el hash se obtiene con `caddy hash-password` en la receta remota.
        files["onboarding_password"] = secrets["admin_password"]
        files["Caddyfile"] = (
            f"https://{configuration['domain']} {{\n{tls} basic_auth {{\n  installer IACP_BCRYPT_PLACEHOLDER\n }}\n reverse_proxy app:5678\n}}\n"
        )
    elif application == "hermes":
        app["volumes"] = ["./data:/opt/data:Z"]
        app["command"] = ["gateway", "run"]
        app["environment"] = env(
            {
                "OPENROUTER_API_KEY": secrets.get("provider_api_key")
                or "local-validation-no-provider"
            }
        )
        files["data/config.yaml"] = json.dumps(
            {
                "model": {
                    "default": configuration.get("model")
                    or "openrouter/local-validation",
                    "provider": "openrouter",
                }
            }
        )
        if secrets.get("telegram_bot_token"):
            app["environment"].update(
                env(
                    {
                        "TELEGRAM_BOT_TOKEN": secrets["telegram_bot_token"],
                        "TELEGRAM_ALLOWED_USERS": ",".join(
                            map(str, configuration["telegram_allowed_users"])
                        ),
                    }
                )
            )
    else:
        app["volumes"] = ["app_data:/home/node/.openclaw"]
        app["environment"] = env(
            {
                "OPENROUTER_API_KEY": secrets.get("provider_api_key")
                or "local-validation-no-provider",
                "OPENCLAW_GATEWAY_TOKEN": secrets["gateway_token"],
            }
        )
        app["command"] = ["node", "openclaw.mjs", "gateway", "--bind", "loopback"]
    if application in {"nextcloud", "n8n"} and configuration.get("access_mode") == "ip":
        # Los clientes que usan una IP omiten SNI; Docker presenta otra IP local a Caddy.
        files["Caddyfile"] = "{\n default_sni " + configuration["domain"] + "\n}\n" + files["Caddyfile"]
    # Sin puertos publicados en agentes: acceso SSH hasta configurar un frontend autenticado.
    return result, files
