#!/usr/bin/env python3
"""Generar configuración revisable sin contactar servicios ni modificar el servidor."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit


def ranges(values):
    if not isinstance(values, list) or not values:
        raise ValueError("Se requieren rangos autorizados de LAN y CRM")
    result = []
    for value in values:
        network = ipaddress.ip_network(value, strict=True)
        if network.prefixlen < (8 if network.version == 4 else 32) or network.is_multicast or network.is_unspecified:
            raise ValueError("Rango demasiado amplio o inválido")
        result.append(str(network))
    return result


def build(policy):
    if set(policy) != {"lan_cidrs", "crm_source_cidrs", "crm_origins"}:
        raise ValueError("Política incompleta o con campos desconocidos")
    allowed = ranges(policy["lan_cidrs"]) + ranges(policy["crm_source_cidrs"])
    origins = policy["crm_origins"]
    if not isinstance(origins, list) or not origins:
        raise ValueError("Se requieren orígenes HTTPS autorizados del backend CRM")
    for origin in origins:
        url = urlsplit(origin)
        _ = url.port
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment or '`' in origin or '*' in origin
                or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", origin)):
            raise ValueError("Origen inválido; utilizar scheme y autoridad HTTPS exactos")
    pattern = "^(?:" + "|".join(re.escape(origin) for origin in origins) + ")$"
    return {
        "http": {
            "routers": {
                "diagnostic": {"rule": "PathPrefix(`/`)", "entryPoints": ["web"], "service": "iacp-api"},
                "iacp-https": {
                    "rule": "Host(`iac.cyberfuel.com`) && (!HeaderRegexp(`Origin`, `.+`) || HeaderRegexp(`Origin`, `" + pattern + "`))",
                    "entryPoints": ["websecure"], "service": "iacp-api",
                    "middlewares": ["authorized-sources", "crm-headers"], "tls": {"options": "corporate"}}},
            "services": {"iacp-api": {"loadBalancer": {"servers": [{"url": "http://api:8000"}]}}},
            "middlewares": {
                "authorized-sources": {"ipAllowList": {"sourceRange": allowed}},
                "crm-headers": {"headers": {
                    "accessControlAllowOriginList": origins,
                    "accessControlAllowMethods": ["GET", "POST", "OPTIONS"],
                    "accessControlAllowHeaders": ["Authorization", "Content-Type", "Idempotency-Key"],
                    "accessControlExposeHeaders": ["X-Request-ID"], "addVaryHeader": True,
                    "contentTypeNosniff": True, "stsSeconds": 31536000}}}},
        "tls": {"certificates": [{"certFile": "/run/tls/fullchain.pem", "keyFile": "/run/tls/privkey.pem"}],
                "options": {"corporate": {"minVersion": "VersionTLS12", "sniStrict": True},
                            "default": {"minVersion": "VersionTLS12", "sniStrict": True}}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--certificates", required=True, type=Path)
    parser.add_argument("--bind-ip", required=True)
    args = parser.parse_args()
    address = ipaddress.ip_address(args.bind_ip)
    if address.version != 4 or not address.is_private or address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Se requiere una IP LAN privada específica")
    configuration = build(json.loads(args.policy.read_text()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certificates / "fullchain.pem", args.certificates / "privkey.pem")
    # JSON es YAML válido; no se necesitan dependencias para interpolar entradas.
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = args.output_dir / "iacp.yml.tmp"
    temporary.write_text(json.dumps(configuration, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(args.output_dir / "iacp.yml")
    print("Configuración TLS generada; revisar SAN, cadena, vigencia, firewall y DNS antes de habilitarla.")


if __name__ == "__main__":
    main()
