#!/usr/bin/env python3
"""Generar configuración revisable sin contactar servicios ni modificar el servidor."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import ssl
import subprocess
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


def build_public(policy):
    """CRM service access is source-bound; all other access requires the gateway.

    Gateway identity never replaces IACP tenant authentication or role checks.
    No response identity header is trusted by the application.
    """
    if set(policy) != {"lan_cidrs", "crm_source_cidrs", "crm_origins", "admin_forward_auth_url"}:
        raise ValueError("Política pública incompleta")
    address = policy["admin_forward_auth_url"]
    url = urlsplit(address)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query
            or url.hostname == "iac.cyberfuel.com" or any(c in address for c in "\r\n`")):
        raise ValueError("Gateway corporativo HTTPS independiente requerido")
    _ = url.port
    base_policy = {k: v for k, v in policy.items() if k != "admin_forward_auth_url"}
    base_policy["crm_origins"] = list(dict.fromkeys(policy["crm_origins"] + ["https://iac.cyberfuel.com"]))
    config = build(base_policy)
    http = config["http"]
    middleware = http["middlewares"]
    middleware.update({
        "request-rate": {"rateLimit": {"average": 10, "burst": 20, "period": "1s"}},
        "inflight": {"inFlightReq": {"amount": 40}},
        "body-limit": {"buffering": {"maxRequestBodyBytes": 65536, "memRequestBodyBytes": 65536}},
        "strip-identity": {"headers": {"customRequestHeaders": {key: "" for key in (
            "X-Forwarded-Prefix", "X-Forwarded-Host", "X-Forwarded-Port", "X-Forwarded-Proto",
            "X-Forwarded-Uri", "X-Forwarded-Method", "X-Auth-Request-User", "X-Auth-Request-Email")}}},
        "corporate-identity": {"forwardAuth": {"address": address, "trustForwardHeader": False,
            "authRequestHeaders": ["Cookie"], "tls": {"insecureSkipVerify": False}}},
        "https-only": {"redirectScheme": {"scheme": "https", "permanent": True}},
    })
    middleware["crm-headers"]["headers"].update(frameDeny=True, referrerPolicy="no-referrer")
    common = ["strip-identity", "request-rate", "inflight", "body-limit", "crm-headers"]
    admin = {"rule": http["routers"]["iacp-https"]["rule"], "priority": 10,
             "entryPoints": ["websecure"], "service": "iacp-api", "tls": {"options": "corporate"},
             "middlewares": common + ["corporate-identity"]}
    # The CRM never reaches approval/recovery/plan/smoke/documentation routes on
    # the source-only lane. Operators at arbitrary locations use the admin lane.
    paths = ("(Path(`/v1/instances`) || PathRegexp(`^/v1/instances/[^/]+(/power(/(start|shutdown|reboot))?)?$`)"
             " || PathRegexp(`^/v1/operations/[^/]+$`) || Path(`/health/live`) || Path(`/health/ready`))")
    sources = " || ".join("ClientIP(`" + network + "`)" for network in ranges(policy["crm_source_cidrs"]))
    http["routers"] = {
        "diagnostic": http["routers"]["diagnostic"],
        "iacp-admin": admin,
        "iacp-crm": {**admin, "rule": admin["rule"] + " && " + paths + " && (" + sources + ")",
                     "priority": 20, "middlewares": common + ["authorized-sources"]},
        "iacp-http": {"rule": "Host(`iac.cyberfuel.com`)", "entryPoints": ["webhttp"],
                      "service": "noop@internal", "middlewares": ["https-only"]},
    }
    return config


def check_certificate(certificates, ca_file=None, min_validity_days=14):
    chain = str(certificates / "fullchain.pem")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, certificates / "privkey.pem")
    commands = [["openssl", "x509", "-in", chain, "-noout", "-checkhost", "iac.cyberfuel.com"],
                ["openssl", "x509", "-in", chain, "-noout", "-checkend", str(min_validity_days * 86400)],
                ["openssl", "verify", "-purpose", "sslserver", "-verify_hostname", "iac.cyberfuel.com",
                 "-untrusted", chain] + (["-CAfile", str(ca_file)] if ca_file else []) + [chain]]
    for command in commands:
        if subprocess.run(command, capture_output=True).returncode:
            raise ValueError("Certificado: SAN, cadena o vigencia inválidos")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--certificates", required=True, type=Path)
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--public", action="store_true", help="Exigir gateway corporativo; generar routers públicos")
    parser.add_argument("--ca-file", type=Path, help="Raíz corporativa aprobada, si no está en el trust store")
    parser.add_argument("--min-validity-days", type=int, default=14)
    args = parser.parse_args()
    address = ipaddress.ip_address(args.bind_ip)
    if address.version != 4 or not address.is_private or address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Se requiere una IP LAN privada específica")
    if args.min_validity_days < 0:
        raise ValueError("Vigencia mínima inválida")
    configuration = (build_public if args.public else build)(json.loads(args.policy.read_text()))
    check_certificate(args.certificates, args.ca_file, args.min_validity_days)
    # JSON es YAML válido; no se necesitan dependencias para interpolar entradas.
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = args.output_dir / "iacp.yml.tmp"
    temporary.write_text(json.dumps(configuration, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(args.output_dir / "iacp.yml")
    print("Configuración TLS generada; revisar SAN, cadena, vigencia, firewall y DNS antes de habilitarla.")


if __name__ == "__main__":
    main()
