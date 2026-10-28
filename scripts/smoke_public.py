#!/usr/bin/env python3
"""Read-only API probe for platform or instance exposure; never disables TLS.

The runner's network vantage is an operator attestation, not inferred from DNS.
No instance creation, queue publishing, Terraform or power command is submitted.
Platform checks do not require a live VM; instance checks run only when an
explicit IACP-owned ``--instance-id`` is supplied.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import time
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

HOST = "iac.cyberfuel.com"
BASE = "https://" + HOST
PATHS = ["/v1/instances", "/v1/instances/{instance_id}", "/v1/instances/{instance_id}/power",
         "/v1/instances/{instance_id}/power/start", "/v1/instances/{instance_id}/power/shutdown",
         "/v1/instances/{instance_id}/power/reboot", "/v1/operations/{operation_id}"]


def exposure_mode(manifest):
    mode = manifest.get("exposure_mode", "public")
    if mode not in {"public", "corporate-internal"}:
        raise ValueError("Unknown exposure_mode")
    return mode


def internal_target(manifest):
    """The API bind is RFC1918; approved sources may also use a private VPN range."""
    private = [ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    address = ipaddress.ip_address(manifest["api_bind_ip"])
    if address.version != 4 or not any(address in net for net in private):
        raise ValueError("Corporate API requires a specific RFC1918 bind IP")
    allowed = private + [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fc00::/7")]
    values = manifest["approved_client_cidrs"]
    networks = [ipaddress.ip_network(cidr, strict=True) for cidr in values]
    if not networks or any(not any(net.version == parent.version and net.subnet_of(parent) for parent in allowed) for net in networks):
        raise ValueError("Explicit private LAN/VPN source networks required")
    if manifest["identity_mode"] not in {"gateway", "approved-internal-policy"}:
        raise ValueError("Corporate identity or approved internal policy required")
    return str(address), networks


def resolved_addresses():
    return sorted({row[4][0] for row in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def private_secret(path):
    if path is None:
        return None
    if path.stat().st_mode & 0o077:
        raise ValueError("Probe credential files must be private (0600)")
    value = path.read_text().strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Invalid credential file")
    return value


def request(path, *, token=None, cookie=None, context=None, method="GET", headers=None, base=BASE):
    # Deliberately ignore ambient proxy settings, and never follow redirects with credentials.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    values = dict(headers or {})
    if token:
        values["Authorization"] = "Bearer " + token
    if cookie:
        values["Cookie"] = cookie
    try:
        response = opener.open(Request(base + path, headers=values, method=method), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read(2 * 1024 * 1024)


def probe(args):
    report = {"passed": False, "at": datetime.now(timezone.utc).isoformat(),
              "runner": args.vantage_id, "external_vantage_asserted": args.external_vantage,
              "host": HOST, "checks": [],
              "instance_smoke": {"status": "not_applicable",
                                  "reason": "No se proporcionó una instancia IACP activa."}}

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise ValueError("Check failed: " + name)

    try:
        manifest = json.loads(args.manifest.read_text()) if getattr(args, "manifest", None) else {}
        mode = exposure_mode(manifest)
        report["exposure_mode"] = mode
        internal = mode == "corporate-internal"
        report["tls_min_validity_days"] = getattr(args, "min_validity_days", 14)
        addresses = resolved_addresses()
        report["resolved_ips"] = addresses
        if internal:
            bind, networks = internal_target(manifest)
            report.update(api_bind_ip=bind, approved_client_cidrs=[str(net) for net in networks],
                          identity_mode=manifest["identity_mode"], lan_vpn_vantage_asserted=args.lan_vpn_vantage)
            check("authorized_lan_vpn_runner", args.lan_vpn_vantage and not args.external_vantage)
            check("corporate_dns", addresses == [bind])
        else:
            check("public_dns", bool(addresses) and all(ipaddress.ip_address(ip).is_global for ip in addresses))
            check("external_runner", args.external_vantage)
            check("approved_public_ips", bool(args.expected_public_ip) and set(addresses) == set(args.expected_public_ip))
        context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        for address in addresses:
            with socket.create_connection((address, 443), timeout=5) as transport:
                with context.wrap_socket(transport, server_hostname=HOST) as connection:
                    if internal:
                        source = connection.getsockname()[0]
                        report["runner_ip"] = source
                        check("approved_lan_vpn_source", any(ipaddress.ip_address(source) in net for net in networks))
                    cert = connection.getpeercert()
                    days = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
                    check("tls_chain_hostname_expiry:" + address, days >= args.min_validity_days)
        status, headers, _ = request("/docs", base="http://" + HOST, context=context)
        check("http_redirect", status in {301, 302, 307, 308} and headers.get("Location") == BASE + "/docs")
        for path in ("/docs", "/openapi.json", "/v1/instances"):
            status, _, _ = request(path, context=context)
            check("anonymous_rejected:" + path, status in {401, 403, 302, 303, 307})
        token = private_secret(args.token_file)
        cookie = private_secret(args.gateway_cookie_file)
        if not internal or manifest["identity_mode"] == "gateway":
            check("gateway_session_available", bool(cookie))
        # A gateway identity alone cannot authorize the IACP contract.
        status, _, _ = request("/openapi.json", cookie=cookie, context=context)
        check("gateway_does_not_replace_iacp", status == 401)
        for path in ("/docs", "/openapi.json", "/health/ready", "/v1/instances"):
            status, headers, body = request(path, token=token, cookie=cookie, context=context)
            check("authorized:" + path, status == 200 and bool(headers.get("X-Request-ID")))
            if path == "/openapi.json":
                contract = json.loads(body)
                check("contract", all(path in contract["paths"] for path in PATHS))
                check("protected_contract", all(operation.get("security") for path in PATHS
                    for method, operation in contract["paths"][path].items() if method in {"get", "post"}))
        instance_id = getattr(args, "instance_id", None)
        if instance_id:
            for path in ("/v1/instances/" + instance_id, "/v1/instances/" + instance_id + "/power"):
                status, headers, body = request(path, token=token, cookie=cookie, context=context)
                check("authorized:" + path, status == 200 and bool(headers.get("X-Request-ID")))
                if path.endswith("/power"):
                    check("guest_readiness_unknown", json.loads(body)["guest_ready"] is None)
            report["instance_smoke"] = {"status": "passed", "instance_id": instance_id}
        if getattr(args, "other_tenant_token_file", None) and instance_id:
            other = private_secret(args.other_tenant_token_file)
            status, _, _ = request("/v1/instances/" + instance_id, token=other, cookie=cookie, context=context)
            check("cross_tenant_hidden", status == 404)
        if getattr(args, "operation_id", None):
            check("operation_tracking", request("/v1/operations/" + args.operation_id,
                token=token, cookie=cookie, context=context)[0] == 200)
        report["passed"] = True
    except Exception as error:
        # Never include an exception body, token, cookie or provider response.
        report["failure"] = str(error) if type(error) is ValueError else type(error).__name__
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vantage-id", required=True)
    parser.add_argument("--external-vantage", action="store_true", help="Operator attests runner is outside the corporate LAN/VPN")
    parser.add_argument("--expected-public-ip", action="append")
    parser.add_argument("--manifest", type=Path, help="Reviewed deployment manifest; defaults to legacy public mode")
    parser.add_argument("--lan-vpn-vantage", action="store_true", help="Operator attests runner is an authorized LAN/VPN client")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--gateway-cookie-file", type=Path)
    parser.add_argument("--other-tenant-token-file", type=Path)
    parser.add_argument("--instance-id", help="UUID de una VM IACP activa para el smoke específico de instancia")
    parser.add_argument("--operation-id")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--min-validity-days", type=int, default=14)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = probe(args)
    with args.output.open("x") as stream:
        args.output.chmod(0o600)
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "report": str(args.output)}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
