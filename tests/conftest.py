import json
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from iacp.api.dependencies import get_instance_publisher, get_plan_publisher, get_smoke_publisher
from iacp.db import get_session
from iacp.main import create_app
from iacp.models import BOOTSTRAP_TENANT_ID
from iacp.services import commercial
from iacp.terraform import profiles
from tests.helpers import upgrade


@pytest.fixture
def empty_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def engine(empty_engine):
    upgrade(empty_engine)
    return empty_engine


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def auth(tmp_path, monkeypatch):
    token = secrets.token_hex(32)
    path = tmp_path / "api_token"
    path.write_text(token)
    monkeypatch.setenv("IACP_API_TOKEN_FILE", str(path))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def application(sessions):
    application = create_app()

    def session_override():
        with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_override
    application.dependency_overrides[get_smoke_publisher] = lambda: lambda job_id: None
    application.dependency_overrides[get_instance_publisher] = lambda: lambda job_id: None
    application.dependency_overrides[get_plan_publisher] = lambda: lambda job_id, attempt: None
    return application


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture(autouse=True)
def core_profile(monkeypatch, tmp_path):
    """Catálogo técnico mínimo común para las pruebas del contrato Core."""
    from tests.test_terraform import allocation_data

    settings = allocation_data(30000)
    for key in ("vm_id", "ipv4_cidr", "ipv4_gateway"):
        settings.pop(key)
    settings.update(
        image="ubuntu-2404", template_vm_id=29000, template_node_name="mng01",
        node_name="mng01", clone_datastore_id="DS_CYBERFUEL",
        cloudinit_datastore_id="DS_CYBERFUEL", network_mode="dhcp",
        network_bridge="vmbr0", vlan_id=None, guest_agent_enabled=True,
        allowed_nodes=["mng01"],
    )
    settings["boot_disk"]["datastore_id"] = "DS_CYBERFUEL"
    path = tmp_path / "core-profiles.json"
    path.write_text(json.dumps({"profiles": {"lab-dhcp": {
        "allowed_tenants": [BOOTSTRAP_TENANT_ID], "settings": settings,
        "vmid_min": 30000, "vmid_max": 30010, "max_live_instances": 3,
        "max_vcpus": 8, "max_memory_mb": 16384, "max_disk_gb": 400,
        "max_total_vcpus": 16, "max_total_memory_mb": 32768,
        "max_total_disk_gb": 800, "max_concurrent_operations": 2,
        "self_service_enabled": True,
        "guest_initialization": "ubuntu-2404-lab-identity-v1",
    }}}))
    monkeypatch.setenv("IACP_TERRAFORM_PROFILES_FILE", str(path))
    monkeypatch.setattr(profiles, "cluster_vmids", lambda: set())
    monkeypatch.setattr(commercial, "live_capacity", lambda *_: None)
    return path
