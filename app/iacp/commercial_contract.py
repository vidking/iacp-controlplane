"""Revisión candidata G de creación. No está conectada a la admisión vigente.

La promoción exige resolver G6 y migrar consumidores. No transforma solicitudes
comerciales en solicitudes históricas con contraseñas dentro de Terraform.
"""
import ipaddress
from typing import Annotated, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .catalog import resolve_image, resolve_plan


class CommercialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class DHCPNetwork(CommercialModel):
    profile_id: Literal["lab-dhcp"]


class StaticNetwork(CommercialModel):
    profile_id: Literal["wan-static"]
    ipv4_address: str
    prefix_length: int = Field(ge=1, le=30)
    gateway: str

    @model_validator(mode="after")
    def coherent_subnet(self):
        address = ipaddress.IPv4Address(self.ipv4_address)
        gateway = ipaddress.IPv4Address(self.gateway)
        subnet = ipaddress.IPv4Network(f"{address}/{self.prefix_length}", strict=False)
        if (gateway not in subnet or address == gateway
                or address in {subnet.network_address, subnet.broadcast_address}
                or gateway in {subnet.network_address, subnet.broadcast_address}
                or address.is_multicast or address.is_loopback or address.is_unspecified
                or gateway.is_multicast or gateway.is_loopback or gateway.is_unspecified):
            raise ValueError("WAN_ADDRESS_INVALID")
        return self


Network = Annotated[DHCPNetwork | StaticNetwork, Field(discriminator="profile_id")]


class CommercialAccess(CommercialModel):
    root_password: SecretStr = Field(min_length=16, max_length=128, repr=False,
        description="Contraseña inicial única; solo entrada HTTPS. Nunca enviar a plan/state ni persistir en PostgreSQL.")
    ssh_public_keys: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("root_password")
    @classmethod
    def valid_password(cls, value):
        if any(ord(c) < 32 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError("ROOT_PASSWORD_FORMAT_INVALID")
        return value

    @field_validator("ssh_public_keys")
    @classmethod
    def public_keys(cls, values):
        identities = set()
        for value in values:
            if len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError("SSH_PUBLIC_KEY_INVALID")
            try:
                key = serialization.load_ssh_public_key(value.encode("ascii"))
                if not (isinstance(key, ed25519.Ed25519PublicKey)
                        or isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048
                        or isinstance(key, ec.EllipticCurvePublicKey) and key.key_size >= 256):
                    raise ValueError()
                identity = key.public_bytes(serialization.Encoding.OpenSSH,
                    serialization.PublicFormat.OpenSSH)
            except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm):
                raise ValueError("SSH_PUBLIC_KEY_INVALID") from None
            if identity in identities:
                raise ValueError("SSH_PUBLIC_KEY_DUPLICATED")
            identities.add(identity)
        return values


class OrderContext(CommercialModel):
    order_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    product_id: str | None = Field(default=None, min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$")


EXAMPLES = [
    {"name": f"cliente-kvm-{n}-001", "plan_id": f"kvm-{n}", "image_id": "ubuntu-2404",
     "network": {"profile_id": "lab-dhcp"},
     "access": {"root_password": "CONTRASENA_ILUSTRATIVA_CAMBIAR", "ssh_public_keys": []},
     "start_on_create": True,
     "commercial_context": {"order_id": f"ORD-100{n}", "product_id": f"CRM-KVM-{n}"}}
    for n in (1, 2)
]


class CommercialInstanceCreate(CommercialModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True,
        json_schema_extra={"examples": EXAMPLES})
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
    plan_id: Literal["kvm-1", "kvm-2", "kvm-3", "kvm-4"]
    image_id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    network: Network
    access: CommercialAccess
    start_on_create: bool = True
    commercial_context: OrderContext


def resolved_resources(spec: CommercialInstanceCreate) -> dict:
    """Resolución pura, sin contraseñas, VMID, asignaciones ni efectos laterales."""
    plan, image = resolve_plan(spec.plan_id), resolve_image(spec.image_id)
    if (plan.vcpus < image.minimum_vcpus or plan.memory_mb < image.minimum_memory_mb
            or plan.disk_gb < image.minimum_disk_gb):
        raise ValueError("IMAGE_PLAN_INCOMPATIBLE")
    return {"vcpus": plan.vcpus, "memory_mb": plan.memory_mb, "disk_gb": plan.disk_gb,
            "image": image.image_id}


def require_safe_execution(spec: CommercialInstanceCreate) -> None:
    """G4/G6 fallan cerrados; ningún flag puede habilitar el adaptador histórico."""
    resolved_resources(spec)
    if spec.network.profile_id == "wan-static":
        raise ValueError("WAN_STATIC_EXECUTION_NOT_CERTIFIED")
    raise ValueError("ROOT_BOOTSTRAP_OUTSIDE_STATE_UNAVAILABLE")
