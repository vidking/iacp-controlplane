#!/usr/bin/env python3
"""Preparar un candidato dev privado; no modifica stacks ni crea credenciales Proxmox."""

import argparse
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from dotenv import dotenv_values


def prepare(
    *,
    output: Path,
    token_file: Path,
    reference: Path,
    image_tag: str,
    endpoint: str,
    host_ip: str,
    pool_id: str,
    runtime_uid: int = 999,
    runtime_gid: int = 999,
    shared_mng: bool = False,
    ipam_env: Path | None = None,
) -> Path:
    from ipaddress import ip_address

    if not re.fullmatch(r"[a-f0-9]{40}", image_tag):
        raise ValueError("image_tag requiere una revisión inmutable de 40 caracteres")
    if (
        urlsplit(endpoint).scheme != "https"
        or urlsplit(endpoint).hostname != "mng01.cyberfuel.com"
    ):
        raise ValueError("Se requiere el endpoint HTTPS canónico de MNG")
    ip_address(host_ip)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", pool_id):
        raise ValueError("pool_id inválido")
    for path in (token_file, reference):
        if not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError("Token y referencia deben tener permisos 600")
    production = dotenv_values(reference, interpolate=False)
    token = token_file.read_text().strip()
    if len(token) < 16 or (
        not shared_mng and token == production.get("IACP_PROXMOX_MNG_API_TOKEN")
    ):
        raise ValueError("Se requiere un token Proxmox exclusivo de desarrollo")
    if not shared_mng and pool_id == production.get("IACP_PROXMOX_MNG_POOL_ID"):
        raise ValueError("Se requiere un pool exclusivo para los ensayos físicos")
    if not all(
        production.get(k)
        for k in ["POSTGRES_PASSWORD", "IACP_API_TOKEN", "IACP_PROXMOX_MNG_API_TOKEN"]
    ):
        raise ValueError("Referencia productiva incompleta")
    if runtime_uid < 1 or runtime_gid < 1:
        raise ValueError("El usuario del runtime no debe ser root")
    if os.geteuid() != 0 and (runtime_uid != os.getuid() or runtime_gid != os.getgid()):
        raise ValueError(
            "Ejecute la preparación como root para asignar el secreto al usuario del contenedor"
        )
    ipam = {}
    if ipam_env:
        if not ipam_env.is_file() or ipam_env.stat().st_mode & 0o777 != 0o600:
            raise ValueError("La configuración IPAM debe tener permisos 600")
        ipam = dotenv_values(ipam_env, interpolate=False)
        if not ipam.get("IACP_NETBOX_TOKEN"):
            raise ValueError("IPAM requiere un token NetBox")
    output = output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    private = output / "services"
    private.mkdir(mode=0o700)
    (private / "key").write_bytes(Fernet.generate_key())
    (private / "key").chmod(0o600)
    os.chown(private, runtime_uid, runtime_gid)
    os.chown(private / "key", runtime_uid, runtime_gid)
    env = {
        "POSTGRES_DB": "iacp_controlplane_dev",
        "POSTGRES_USER": "iacp_dev",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "IACP_API_TOKEN": secrets.token_urlsafe(48),
        "IACP_IMAGE_REPOSITORY": "scm.cyberfuel.com/cyberfuel/iacp-controlplane",
        "IACP_IMAGE_TAG": image_tag,
        "IACP_RUNTIME_STAGE": "develop",
        "IACP_DEFAULT_ENVIRONMENT": "lab",
        "IACP_ENABLED_ENVIRONMENTS": "lab",
        "IACP_WASABI_ENABLED": "false",
        "IACP_MAX_CONCURRENT": "6",
        "IACP_SERVICE_MAX_CONCURRENT": "4",
        "IACP_ENFORCE_WAN_ONLY": "0",
        "IACP_VMID_MIN": "40000",
        "IACP_VMID_MAX": "49999",
        "IACP_PROXMOX_MNG_ENDPOINT": endpoint,
        "IACP_PROXMOX_MNG_HOSTNAME": "mng01.cyberfuel.com",
        "IACP_PROXMOX_MNG_HOST_IP": host_ip,
        "IACP_PROXMOX_MNG_API_TOKEN": token,
        "IACP_PROXMOX_MNG_POOL_ID": pool_id,
        "IACP_MNG_CATALOG": "/app/terraform/environments/mng01/catalog-dev.json",
        "IACP_NETBOX_EXCLUDED_ADDRESSES": "190.0.231.2",
        "IACP_POSTGRES_VOLUME": "iacp_controlplane_dev_postgres",
        "IACP_WORKDATA_VOLUME": "iacp_controlplane_dev_workdata",
        "IACP_PROXY_SERVICE_ALIAS": "iacp-controlplane-api-dev",
        "IACP_PROXY_NETWORK": "proxy_network",
        "IACP_PROD_STATIC_ENABLED": "0",
        "IACP_SERVICES_ENABLED": "1",
        "IACP_SERVICE_EXECUTION_ENABLED": "0",
        "IACP_SERVICE_CONFIG_DIR": str(private),
        "IACP_SERVICE_RELEASES_FILE": "/app/services/releases.json",
    }
    for key in [
        "IACP_NETBOX_URL",
        "IACP_NETBOX_TOKEN",
        "IACP_NETBOX_PREFIX_LENGTH",
        "IACP_NETBOX_GATEWAY_HOST",
        "IACP_NETBOX_ALLOCATION_TAG",
        "IACP_NETBOX_GATEWAY_TAG",
        "IACP_NETBOX_VERIFY_TLS",
    ]:
        if ipam.get(key) is not None:
            env[key] = ipam[key]
    # IPAM solo se habilita después de revisar red y permisos en el preflight.
    # Comillas simples evitan interpolación Compose de $, # y espacios en credenciales.
    if any("\n" in value or "\r" in value or "'" in value for value in env.values()):
        raise ValueError(
            "Un valor de configuración no puede representarse de forma segura"
        )
    path = output / "dev.env"
    path.write_text("".join(f"{key}='{value}'\n" for key, value in env.items()))
    path.chmod(0o600)
    template = (
        Path(__file__).resolve().parents[1]
        / "deploy/portainer/manifest-dev.example.json"
    )
    manifest = json.loads(template.read_text())
    manifest.update(
        mng_credentials_shared=shared_mng,
        environment_file=str(path),
        image_tag=image_tag,
        isolation_reference_environment_file=str(reference.resolve()),
    )
    path = output / "manifest-dev.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    path.chmod(0o600)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--proxmox-token-file", type=Path, required=True)
    parser.add_argument("--reference-env", type=Path, required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--proxmox-endpoint", required=True)
    parser.add_argument("--proxmox-host-ip", required=True)
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--runtime-uid", type=int, default=999)
    parser.add_argument("--runtime-gid", type=int, default=999)
    parser.add_argument(
        "--shared-mng",
        action="store_true",
        help="Compartir únicamente token y pool Proxmox de MNG, con autorización expresa",
    )
    parser.add_argument("--ipam-env", type=Path)
    args = parser.parse_args()
    try:
        path = prepare(
            output=args.output_dir,
            token_file=args.proxmox_token_file,
            reference=args.reference_env,
            image_tag=args.image_tag,
            endpoint=args.proxmox_endpoint,
            host_ip=args.proxmox_host_ip,
            pool_id=args.pool_id,
            runtime_uid=args.runtime_uid,
            runtime_gid=args.runtime_gid,
            shared_mng=args.shared_mng,
            ipam_env=args.ipam_env,
        )
    except (ValueError, OSError):
        parser.exit(
            2,
            "No se pudo preparar el candidato; revise rutas, permisos, identidad e independencia de credenciales.\n",
        )
    print(f"Candidato privado preparado: {path}")


if __name__ == "__main__":
    main()
