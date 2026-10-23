import base64
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from iacp.models import Job, Operation

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load("go_live", "go_live.py")
tls = load("go_live_tls", "render-tls.py")


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_require_tenant_authentication(client, auth, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.get(path, headers=auth)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    password = auth["Authorization"].removeprefix("Bearer ")
    basic = base64.b64encode(("iacp:" + password).encode()).decode()
    assert client.get(path, headers={"Authorization": "Basic " + basic}).status_code == 200
    assert client.get("/v1/instances", headers={"Authorization": "Basic " + basic}).status_code == 401


def test_drain_blocks_all_mutations_without_queue_or_db_writes(client, auth, sessions, monkeypatch):
    monkeypatch.setenv("IACP_ADMISSION_ENABLED", "0")
    for path in ("/v1/jobs/smoke", "/v1/instances", "/v1/instances/missing/power/start"):
        response = client.post(path, headers=auth, json={})
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
    assert client.get("/v1/instances", headers=auth).status_code == 200
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(Operation)) == 0


def test_request_id_cannot_be_spoofed_and_logs_do_not_contain_credentials(client, auth, caplog):
    with caplog.at_level("INFO", logger="uvicorn.error"):
        response = client.get("/v1/instances?secret=do-not-log", headers={**auth, "X-Request-ID": "spoofed"})
    assert response.headers["X-Request-ID"] != "spoofed"
    assert auth["Authorization"] not in caplog.text
    assert "do-not-log" not in caplog.text
    event = json.loads(caplog.records[-1].message)
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["route"] == "/v1/instances"


def test_idle_and_history_guards_preserve_uncertainty():
    original = {"nonterminal": {}, "queues": {}, "unacked": 0,
                "tables": {"terraform_attempts": {"count": 4, "sha256": "original"}}}
    driver.check_idle(original)
    for changed in ({"nonterminal": {"operations": [{"status": "uncertain"}]}},
                    {"queues": {"lifecycle": 1}}, {"unacked": 1}):
        with pytest.raises(RuntimeError):
            driver.check_idle({**original, **changed})
    with pytest.raises(RuntimeError, match="Historical rows changed"):
        driver.compare_history(original, {"tables": {"terraform_attempts": {"count": 4, "sha256": "rewritten"}}})


@pytest.mark.parametrize("action", ["promote", "publish", "activate", "first_start", "rollback", "certify"])
def test_expired_change_cannot_mutate_runtime(tmp_path, action):
    change = tmp_path / "change.json"
    change.write_text(json.dumps({"window_start": "2020-01-01T00:00:00Z", "window_end": "2020-01-02T00:00:00Z"}))
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"change_record": {"path": str(change), "sha256": driver.digest(change)}}
    commands = []
    promotion.compose = lambda *args, **kwargs: commands.append(args)
    with pytest.raises(RuntimeError, match="Outside authorized change window"):
        getattr(promotion, action)()
    assert commands == []


def test_external_probe_rejects_private_dns_before_reading_tokens(monkeypatch, tmp_path):
    from argparse import Namespace
    probe = load("public_probe", "smoke_public.py")
    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("192.168.1.50", 443))])
    report = probe.probe(Namespace(vantage_id="unit-fixture", external_vantage=True, expected_public_ip=["192.168.1.50"]))
    assert report["passed"] is False
    assert report["checks"] == [{"name": "public_dns", "passed": False}]


def test_public_router_keeps_crm_away_from_docs_and_admin_paths():
    policy = {"lan_cidrs": ["192.0.2.0/24"], "crm_source_cidrs": ["198.51.100.1/32"],
              "crm_origins": ["https://crm.fixture.invalid"], "admin_forward_auth_url": "https://identity.fixture.invalid/verify"}
    config = tls.build_public(policy)["http"]
    admin = config["routers"]["iacp-admin"]
    crm = config["routers"]["iacp-crm"]
    assert "corporate-identity" in admin["middlewares"]
    assert "corporate-identity" not in crm["middlewares"]
    assert "ClientIP(`198.51.100.1/32`)" in crm["rule"]
    assert "/docs" not in crm["rule"] and "/terraform" not in crm["rule"]
    auth = config["middlewares"]["corporate-identity"]["forwardAuth"]
    assert auth["authRequestHeaders"] == ["Cookie"]
    assert auth["trustForwardHeader"] is False
    assert auth["tls"]["insecureSkipVerify"] is False
    for url in ("http://identity.invalid", "https://iac.cyberfuel.com/auth", "https://user:secret@identity.invalid"):
        with pytest.raises(ValueError):
            tls.build_public({**policy, "admin_forward_auth_url": url})
