"""Contrato aditivo para pedidos de aplicaciones."""

from typing import Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from api.models import InstanceCreateRequest
from api.service_catalog import supported_image_ids


class ApplicationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str | None = Field(
        default=None, max_length=253, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$"
    )
    access_mode: Literal["domain", "ip"] = "domain"
    agent_mode: Literal["connected", "local"] = "connected"
    admin_user: str = Field(default="admin", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    model: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-zA-Z0-9_./:-]+$"
    )
    telegram_allowed_users: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value):
        if value and (
            "." not in value
            or any(
                not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
                for part in value.split(".")
            )
        ):
            raise ValueError("domain requiere un FQDN válido")
        return value


class ApplicationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_api_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=8192
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None, min_length=1, max_length=1024
    )


class ServiceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: Literal["nextcloud", "n8n", "hermes", "openclaw"]
    instance: InstanceCreateRequest
    configuration: ApplicationConfiguration = Field(
        default_factory=ApplicationConfiguration
    )
    secrets: ApplicationSecrets = Field(default_factory=ApplicationSecrets)

    @model_validator(mode="after")
    def compatible(self):
        supported_images = supported_image_ids(self.application_id)
        if (
            self.instance.image_id not in supported_images
            or not self.instance.start_on_create
        ):
            raise ValueError(
                "la imagen no está habilitada para esta aplicación o start_on_create no es true"
            )
        if (
            not self.instance.access.ssh_public_keys
            and not self.instance.access.root_password
        ):
            raise ValueError(
                "se requiere root_password o una clave SSH del cliente para conservar acceso al sistema"
            )
        if (
            self.application_id in {"nextcloud", "n8n"}
            and self.configuration.access_mode == "domain"
            and not self.configuration.domain
        ):
            raise ValueError("la aplicación requiere domain")
        if self.configuration.access_mode == "ip":
            if self.configuration.domain:
                raise ValueError("access_mode=ip no admite domain")
        if (
            self.application_id in {"hermes", "openclaw"}
            and self.configuration.agent_mode == "connected"
        ):
            if not self.secrets.provider_api_key or not self.configuration.model:
                raise ValueError(
                    "el agente requiere model y provider_api_key (OpenRouter en v1)"
                )
        if self.secrets.telegram_bot_token and (
            self.application_id != "hermes"
            or not self.configuration.telegram_allowed_users
        ):
            raise ValueError(
                "Telegram v1 requiere Hermes y usuarios autorizados explícitos"
            )
        return self


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_execution_stopped: bool = False


class ApplicationConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secrets: ApplicationSecrets
    telegram_allowed_users: list[int] | None = Field(default=None, max_length=100)
