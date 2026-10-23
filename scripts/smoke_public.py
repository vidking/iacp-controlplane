#!/usr/bin/env python3
"""Read-only public API probe. Run on an approved external runner; never disables TLS.

The runner's external location is an operator attestation, not inferred from DNS.
No instance creation, queue publishing, Terraform or power command is submitted.
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
              "host": HOST, "checks": []}

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise ValueError("Check failed: " + name)

    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)})
        report["resolved_ips"] = addresses
        check("public_dns", bool(addresses) and all(ipaddress.ip_address(ip).is_global for ip in addresses))
        check("approved_public_ips", set(addresses) == set(args.expected_public_ip))
        context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        for address in addresses:
            with socket.create_connection((address, 443), timeout=5) as transport:
                with context.wrap_socket(transport, server_hostname=HOST) as connection:
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
        # A gateway identity alone cannot authorize the IACP contract.
        status, _, _ = request("/openapi.json", cookie=cookie, context=context)
        check("gateway_does_not_replace_iacp", status == 401)
        for path in ("/docs", "/openapi.json", "/health/ready", "/v1/instances",
                     "/v1/instances/" + args.instance_id, "/v1/instances/" + args.instance_id + "/power"):
            status, headers, body = request(path, token=token, cookie=cookie, context=context)
            check("authorized:" + path, status == 200 and bool(headers.get("X-Request-ID")))
            if path == "/openapi.json":
                contract = json.loads(body)
                check("contract", all(path in contract["paths"] for path in PATHS))
                check("protected_contract", all(operation.get("security") for path in PATHS
                    for method, operation in contract["paths"][path].items() if method in {"get", "post"}))
            if path.endswith("/power"):
                check("guest_readiness_unknown", json.loads(body)["guest_ready"] is None)
        if args.other_tenant_token_file:
            other = private_secret(args.other_tenant_token_file)
            status, _, _ = request("/v1/instances/" + args.instance_id, token=other, cookie=cookie, context=context)
            check("cross_tenant_hidden", status == 404)
        if args.operation_id:
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
    parser.add_argument("--external-vantage", action="store_true", required=True, help="Operator attests runner is outside the corporate LAN/VPN")
    parser.add_argument("--expected-public-ip", action="append", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--gateway-cookie-file", type=Path, required=True)
    parser.add_argument("--other-tenant-token-file", type=Path)
    parser.add_argument("--instance-id", default="a1b2d063-6240-4969-bc04-e7e0e6be9e73")
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
