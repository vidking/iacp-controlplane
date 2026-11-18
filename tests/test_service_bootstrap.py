"""Identidad de bindings y preparación privada; no ejecuta netplan en el host."""

import ast
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from api.main import app
from api.db import database, initialize
from worker import service_bootstrap as bootstrap
from worker.guest_identity import GuestIdentityError
from scripts.prepare_develop import prepare

MAC = "aa:bb:cc:dd:ee:ff"


def order(client):
    response = client.post(
        "/v1/service-instances",
        headers={"Idempotency-Key": "bootstrap-test"},
        json={
            "application_id": "nextcloud",
            "instance": {
                "name": "bootstrap-test",
                "plan_id": "kvm-2",
                "image_id": "ubuntu-2404",
                "access": {"ssh_public_keys": ["ssh-ed25519 AAAAexample"]},
            },
            "configuration": {"domain": "cloud.example.org"},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_binding_is_observed_on_correct_mac_and_persisted(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("IACP_SERVICE_KEY_FILE", str(key))
    monkeypatch.setenv("IACP_SERVICES_ENABLED", "1")
    monkeypatch.setattr(
        bootstrap, "_wait_for_pool_membership", lambda *_args, **_kwargs: None
    )
    with TestClient(app) as client:
        created = order(client)
    response = {
        "instance_id": created["instance_id"],
        "mac_address": MAC,
        "host_key": "ssh-ed25519 AAAAexample",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
    }
    fake = SimpleNamespace(
        vm_config=lambda *_: {
            "name": "bootstrap-test",
            "ipconfig0": "ip=dhcp",
            "net0": "virtio=" + MAC + ",bridge=vmbr0",
        },
        guest_agent_exec=lambda *_: {"pid": 10},
        guest_agent_exec_status=lambda *_: {
            "exited": True,
            "exitcode": 0,
            "out-data": json.dumps(response),
        },
        guest_agent_interfaces=lambda *_: [
            {"hardware-address": MAC, "ip-addresses": [{"ip-address": "192.0.2.20"}]},
            {
                "hardware-address": "00:00:00:00:00:00",
                "ip-addresses": [{"ip-address": "172.17.0.1"}],
            },
        ],
    )
    binding = bootstrap.prepare(
        fake,
        node="mng01",
        vmid=40001,
        name="bootstrap-test",
        instance_id=created["instance_id"],
        heartbeat=lambda: None,
    )
    assert binding["address"] == "192.0.2.20"
    assert binding["source"] == "qemu-guest-agent"
    with database() as db:
        persisted = db.execute("SELECT binding_json FROM service_bindings").fetchone()
        spec = json.loads(
            db.execute("SELECT spec_json FROM instances").fetchone()["spec_json"]
        )
    assert json.loads(persisted["binding_json"]) == binding
    assert spec["dns_servers"] == ["1.1.1.1", "8.8.8.8"]
    response["instance_id"] = "different-instance"
    with pytest.raises(GuestIdentityError, match="IDENTITY_MISMATCH"):
        bootstrap.prepare(
            fake,
            node="mng01",
            vmid=40001,
            name="bootstrap-test",
            instance_id=created["instance_id"],
            heartbeat=lambda: None,
        )
    ast.parse(bootstrap.PROGRAM)


def test_no_service_means_no_guest_commands():
    initialize()
    assert (
        bootstrap.prepare(
            object(),
            node="mng01",
            vmid=40001,
            name="plain-kvm",
            instance_id="not-a-service",
            heartbeat=lambda: None,
        )
        is None
    )


def test_prepare_develop_is_private_and_non_destructive(tmp_path):
    reference = tmp_path / "prod.env"
    reference.write_text(
        "POSTGRES_PASSWORD=old-database\nIACP_API_TOKEN=old-api-token\nIACP_PROXMOX_MNG_API_TOKEN=old-proxmox-token\nIACP_PROXMOX_MNG_POOL_ID=prod-pool\n"
    )
    reference.chmod(0o600)
    token = tmp_path / "token"
    token.write_text("exclusive-dev-proxmox-token")
    token.chmod(0o600)
    args = dict(
        output=tmp_path / "candidate",
        token_file=token,
        reference=reference,
        image_tag="a" * 40,
        endpoint="https://mng01.cyberfuel.com:8006/",
        host_ip="192.0.2.10",
        pool_id="dev-pool",
        runtime_uid=__import__("os").getuid(),
        runtime_gid=__import__("os").getgid(),
    )
    path = prepare(**args)
    manifest = json.loads(path.read_text())
    assert (
        manifest["domain"] == "dev-iac.cyberfuel.com"
        and manifest["schema_version"] == 3
    )
    assert manifest["max_concurrent"] == 6 and manifest["services_enabled"]
    assert path.stat().st_mode & 0o777 == 0o600
    from dotenv import dotenv_values

    env = dotenv_values(path.parent / "dev.env", interpolate=False)
    assert env["IACP_SERVICE_EXECUTION_ENABLED"] == "0"
    assert env["POSTGRES_PASSWORD"] != "old-database"
    assert reference.read_text().startswith("POSTGRES_PASSWORD=old-database")
    with pytest.raises(FileExistsError):
        prepare(**args)


def test_prepare_develop_rejects_shared_proxmox_token(tmp_path):
    reference = tmp_path / "prod.env"
    reference.write_text("IACP_PROXMOX_MNG_API_TOKEN=shared-token-123456789\n")
    reference.chmod(0o600)
    token = tmp_path / "token"
    token.write_text("shared-token-123456789")
    token.chmod(0o600)
    with pytest.raises(ValueError, match="exclusivo"):
        prepare(
            output=tmp_path / "candidate",
            token_file=token,
            reference=reference,
            image_tag="a" * 40,
            endpoint="https://mng01.cyberfuel.com:8006/",
            host_ip="192.0.2.10",
            pool_id="dev-pool",
            runtime_uid=__import__("os").getuid(),
            runtime_gid=__import__("os").getgid(),
        )
    assert not (tmp_path / "candidate").exists()


def test_static_bootstrap_rejects_wrong_bridge_or_tag(monkeypatch):
    initialize()
    monkeypatch.setenv("IACP_PROD_STATIC_BRIDGE", "vmbr3")
    monkeypatch.setattr(
        bootstrap, "_wait_for_pool_membership", lambda *_args, **_kwargs: None
    )
    spec = {
        "network_profile": "prod_static",
        "ipv4_cidr": "190.0.231.8/24",
        "ipv4_gateway": "190.0.231.1",
        "vlan_id": None,
    }
    config = {
        "name": "static-test",
        "net0": "virtio=" + MAC + ",bridge=vmbr3",
        "ipconfig0": "ip=190.0.231.8/24,gw=190.0.231.1",
    }
    response = {
        "instance_id": "static-id",
        "mac_address": MAC,
        "host_key": "ssh-ed25519 AAAAexample",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
    }
    fake = SimpleNamespace(
        vm_config=lambda *_: config,
        guest_agent_exec=lambda *_: {"pid": 10},
        guest_agent_exec_status=lambda *_: {
            "exited": True,
            "exitcode": 0,
            "out-data": json.dumps(response),
        },
        guest_agent_interfaces=lambda *_: [
            {"hardware-address": MAC, "ip-addresses": [{"ip-address": "190.0.231.8"}]}
        ],
    )
    result = bootstrap.prepare(
        fake,
        node="mng01",
        vmid=40003,
        name="static-test",
        instance_id="static-id",
        spec=spec,
        heartbeat=lambda: None,
    )
    assert result["address"] == "190.0.231.8"
    config["net0"] = "virtio=" + MAC + ",bridge=vmbr0"
    with pytest.raises(GuestIdentityError, match="STATIC_IDENTITY_MISMATCH"):
        bootstrap.prepare(
            fake,
            node="mng01",
            vmid=40003,
            name="static-test",
            instance_id="static-id",
            spec=spec,
            heartbeat=lambda: None,
        )
    config["net0"] = "virtio=" + MAC + ",bridge=vmbr3,tag=7"
    with pytest.raises(GuestIdentityError, match="STATIC_IDENTITY_MISMATCH"):
        bootstrap.prepare(
            fake,
            node="mng01",
            vmid=40003,
            name="static-test",
            instance_id="static-id",
            spec=spec,
            heartbeat=lambda: None,
        )


def test_static_bootstrap_accepts_vmbr3_without_tag(monkeypatch):
    initialize()
    monkeypatch.setenv("IACP_PROD_STATIC_BRIDGE", "vmbr3")
    monkeypatch.setattr(
        bootstrap, "_wait_for_pool_membership", lambda *_args, **_kwargs: None
    )
    config = {
        "name": "wan-untagged-test",
        "net0": "virtio=" + MAC + ",bridge=vmbr3",
        "ipconfig0": "ip=190.0.231.8/24,gw=190.0.231.1",
    }
    response = {
        "instance_id": "wan-untagged-id",
        "mac_address": MAC,
        "host_key": "ssh-ed25519 AAAAexample",
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
    }
    fake = SimpleNamespace(
        vm_config=lambda *_: config,
        guest_agent_exec=lambda *_: {"pid": 10},
        guest_agent_exec_status=lambda *_: {
            "exited": True,
            "exitcode": 0,
            "out-data": json.dumps(response),
        },
        guest_agent_interfaces=lambda *_: [
            {"hardware-address": MAC, "ip-addresses": [{"ip-address": "190.0.231.8"}]}
        ],
    )
    result = bootstrap.prepare(
        fake,
        node="mng01",
        vmid=40004,
        name="wan-untagged-test",
        instance_id="wan-untagged-id",
        spec={
            "network_profile": "prod_static",
            "ipv4_cidr": "190.0.231.8/24",
            "ipv4_gateway": "190.0.231.1",
            "vlan_id": None,
        },
        heartbeat=lambda: None,
    )
    assert result["address"] == "190.0.231.8"


def test_static_guest_readiness_requires_gateway_neighbor():
    # Ejecuta el predicado real del programa remoto con observaciones de iproute2.
    tree = ast.parse(bootstrap.PROGRAM)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'gateway_ready')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<gateway-probe>', 'exec'), namespace)
    ready = namespace['gateway_ready']
    assert not ready([{'dst':'190.0.231.1','dev':'eth0','state':['INCOMPLETE']}], '190.0.231.1','eth0')
    assert ready([{'dst':'190.0.231.1','dev':'eth0','state':['STALE'],'lladdr':'aa:bb:cc:dd:ee:ff'}], '190.0.231.1','eth0')
    assert not ready([{'dst':'190.0.231.1','dev':'eth1','state':['REACHABLE'],'lladdr':'aa:bb:cc:dd:ee:ff'}], '190.0.231.1','eth0')


def test_dhcp_bootstrap_refuses_tagged_nic(monkeypatch):
    initialize()
    fake=SimpleNamespace(vm_config=lambda *_:{'name':'dhcp-test','net0':'virtio='+MAC+',bridge=vmbr0,tag=9','ipconfig0':'ip=dhcp'})
    with pytest.raises(GuestIdentityError,match='DHCP_UNEXPECTED_NIC_TAG'):
        bootstrap.prepare(fake,node='mng01',vmid=40012,name='dhcp-test',instance_id='dhcp-test',spec={'network_profile':'lab_dhcp','prepare_identity':True},heartbeat=lambda:None)


def test_agent_wait_retries_reads_but_never_replays_uncertain_exec(monkeypatch):
    from worker.proxmox import ProxmoxError
    initialize()
    monkeypatch.setattr(bootstrap,'_wait_for_pool_membership',lambda *args,**kwargs:None)
    monkeypatch.setattr(bootstrap.time,'sleep',lambda _:None)
    calls={'reads':0,'exec':0}
    def interfaces(*_):
        calls['reads']+=1
        if calls['reads']==1:raise ProxmoxError('AGENT_NOT_READY')
        return []
    def execute(*_):
        calls['exec']+=1
        raise ProxmoxError('RESULT_UNKNOWN',uncertain=True)
    fake=SimpleNamespace(vm_config=lambda *_:{'name':'wait-test','net0':'virtio='+MAC+',bridge=vmbr0,tag=211','ipconfig0':'ip=190.0.231.8/24,gw=190.0.231.1'},guest_agent_interfaces=interfaces,guest_agent_exec=execute)
    with pytest.raises(ProxmoxError,match='RESULT_UNKNOWN'):
        bootstrap.prepare(fake,node='mng01',vmid=40012,name='wait-test',instance_id='wait-test',spec={'network_profile':'prod_static','ipv4_cidr':'190.0.231.8/24','ipv4_gateway':'190.0.231.1'},heartbeat=lambda:None)
    assert calls=={'reads':2,'exec':1}


def test_binding_waits_for_dhcp_address_after_network_apply(monkeypatch):
    initialize()
    monkeypatch.setattr(bootstrap,'_wait_for_pool_membership',lambda *args,**kwargs:None)
    sleeps=[]
    monkeypatch.setattr(bootstrap.time,'sleep',sleeps.append)
    interface={'hardware-address':MAC,'ip-addresses':[{'ip-address':'192.0.2.20'}]}
    previous={'hardware-address':MAC,'ip-addresses':[{'ip-address':'192.0.2.19'}]}
    observations=iter([[interface],[],[previous],[interface]])
    response={'instance_id':'lease-test','mac_address':MAC,'host_key':'ssh-ed25519 AAAAexample','dns_servers':['1.1.1.1','8.8.8.8'],'ipv4':'192.0.2.20'}
    fake=SimpleNamespace(vm_config=lambda *_:{'name':'lease-test','net0':'virtio='+MAC+',bridge=vmbr0','ipconfig0':'ip=dhcp'},guest_agent_interfaces=lambda *_:next(observations),guest_agent_exec=lambda *_:{'pid':10},guest_agent_exec_status=lambda *_:{'exited':True,'exitcode':0,'out-data':json.dumps(response)})
    binding=bootstrap.prepare(fake,node='mng01',vmid=40012,name='lease-test',instance_id='lease-test',spec={'network_profile':'lab_dhcp','prepare_identity':True},heartbeat=lambda:None)
    assert binding['address']=='192.0.2.20'
    assert sleeps==[2,2]


def test_dhcp_lease_must_identify_actual_mac_before_certificate_binding():
    tree=ast.parse(bootstrap.PROGRAM)
    function=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='mac_lease')
    namespace={}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<lease-proof>','exec'),namespace)
    proof=namespace['mac_lease']
    assert proof('CLIENTID=01:aa:bb:cc:dd:ee:ff\nADDRESS=192.0.2.20\n',MAC)=='192.0.2.20'
    assert proof('CLIENTID=ff:aa:bb:cc:dd:ee:ff\nADDRESS=192.0.2.20\n',MAC) is None
    assert proof('CLIENTID=01:11:22:33:44:55:66\nADDRESS=192.0.2.20\n',MAC) is None


def test_almalinux_requires_fixed_helper_instead_of_arbitrary_exec(monkeypatch):
    initialize()
    monkeypatch.setattr(bootstrap,'_wait_for_pool_membership',lambda *args,**kwargs:None)
    commands=[]
    fake=SimpleNamespace(vm_config=lambda *_:{'name':'alma-test','net0':'e1000='+MAC+',bridge=vmbr0','ipconfig0':'ip=dhcp'},guest_agent_interfaces=lambda *_:[],guest_agent_exec=lambda _n,_v,argv:commands.append(argv) or {'pid':10},guest_agent_exec_status=lambda *_:{'exited':True,'exitcode':1})
    with pytest.raises(GuestIdentityError,match='IMAGE_BOOTSTRAP_REQUIRED'):
        bootstrap.prepare(fake,node='mng01',vmid=40012,name='alma-test',instance_id='alma-test',spec={'image':'almalinux-9','network_profile':'lab_dhcp','prepare_identity':True},heartbeat=lambda:None)
    assert commands==[['/usr/bin/test','-x','/usr/local/libexec/iacp-guest-bootstrap']]


def test_dns_falls_back_to_networkmanager_when_resolved_is_inactive():
    tree = ast.parse(bootstrap.PROGRAM)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == 'observed_dns'
    )
    calls = []
    namespace = {
        'shutil': SimpleNamespace(which=lambda name: '/usr/bin/' + name),
        'subprocess': SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(returncode=1)),
        'run': lambda argv: calls.append(argv) or '1.1.1.1\n8.8.8.8\n',
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<dns-proof>', 'exec'), namespace)
    assert namespace['observed_dns']('eth0') == '1.1.1.1\n8.8.8.8\n'
    assert calls == [['/usr/bin/nmcli', '-g', 'IP4.DNS', 'device', 'show', 'eth0']]


def test_cloud_init_json_handles_wait_output_and_rejects_real_errors():
    tree = ast.parse(bootstrap.PROGRAM)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'cloud_ready')
    namespace = {'json': json}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<cloud-init-proof>', 'exec'), namespace)
    status = {'status': 'done', 'errors': [], 'recoverable_errors': {'DEPRECATED': ["'user' of type string is deprecated"]}}
    assert namespace['cloud_ready'](SimpleNamespace(returncode=2, stdout=json.dumps(status))) is None
    status['recoverable_errors'] = {'WARNING': ['No se pudo ejecutar un módulo']}
    with pytest.raises(AssertionError):
        namespace['cloud_ready'](SimpleNamespace(returncode=0, stdout=json.dumps(status)))
    status['recoverable_errors'] = {}
    status['errors'] = ['Fallo de configuración']
    with pytest.raises(AssertionError):
        namespace['cloud_ready'](SimpleNamespace(returncode=1, stdout=json.dumps(status)))
