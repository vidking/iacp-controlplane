"""Cortes de conexión con Proxmox y NetBox durante la respuesta.

``urllib`` solo envuelve en ``URLError`` los fallos al enviar. Una conexión
cerrada o truncada mientras se lee la respuesta llega como ``OSError`` o
``http.client.HTTPException`` y debe seguir el mismo contrato: reintentar las
lecturas y declarar inciertas las mutaciones.
"""

from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from types import SimpleNamespace

import pytest

from worker.netbox import NetBoxClient, NetBoxError
from worker.proxmox import ProxmoxClient, ProxmoxError

pytestmark = pytest.mark.no_database

CUTS = [
    RemoteDisconnected("conexión cerrada sin respuesta"),
    ConnectionResetError(104, "connection reset by peer"),
    IncompleteRead(b'{"data":', 500),
    BadStatusLine("respuesta ilegible"),
]
CUT_IDS = [type(error).__name__ for error in CUTS]


def failing_opener(error, requests):
    def open_request(request, **_kwargs):
        requests.append(request.get_method())
        raise error

    return SimpleNamespace(open=open_request)


def proxmox_client(error, requests):
    client = ProxmoxClient("https://mng.test.invalid:8006", "usuario@pve!token=valor")
    client.opener = failing_opener(error, requests)
    return client


def netbox_client(error, requests):
    client = object.__new__(NetBoxClient)
    client.config = SimpleNamespace(
        token="token-de-prueba",
        base_url="https://netbox.test.invalid/api",
        attempts=3,
        timeout=1,
    )
    client.opener = failing_opener(error, requests)
    return client


@pytest.mark.parametrize("error", CUTS, ids=CUT_IDS)
def test_proxmox_reintenta_lecturas_cortadas(monkeypatch, error):
    monkeypatch.setattr("worker.proxmox.time.sleep", lambda _: None)
    requests = []
    with pytest.raises(ProxmoxError) as failure:
        proxmox_client(error, requests).get("/nodes", attempts=3)
    assert requests == ["GET"] * 3
    assert failure.value.uncertain is False


@pytest.mark.parametrize("error", CUTS, ids=CUT_IDS)
def test_proxmox_no_repite_mutaciones_cortadas_y_las_declara_inciertas(error):
    requests = []
    with pytest.raises(ProxmoxError) as failure:
        proxmox_client(error, requests).post("/nodes/mng01/qemu/40000/status/start")
    assert requests == ["POST"]
    assert failure.value.uncertain is True


@pytest.mark.parametrize("error", CUTS, ids=CUT_IDS)
def test_netbox_reintenta_lecturas_cortadas(monkeypatch, error):
    monkeypatch.setattr("worker.netbox.time.sleep", lambda _: None)
    requests = []
    with pytest.raises(NetBoxError, match="IPAM_UNAVAILABLE") as failure:
        netbox_client(error, requests)._request(
            "GET", "/ipam/ip-addresses/", retry_get=True
        )
    assert requests == ["GET"] * 3
    assert failure.value.uncertain is False


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("error", CUTS, ids=CUT_IDS)
def test_netbox_no_repite_mutaciones_cortadas_y_las_declara_inciertas(error, method):
    requests = []
    with pytest.raises(NetBoxError, match="IPAM_REQUEST_UNCERTAIN") as failure:
        netbox_client(error, requests)._request(method, "/ipam/ip-addresses/1/")
    assert requests == [method]
    assert failure.value.uncertain is True


def test_observacion_fallida_tras_orden_de_energia_queda_incierta(monkeypatch):
    client = object.__new__(ProxmoxClient)

    def unreadable(*_args):
        raise ProxmoxError("Proxmox GET /status falló: <urlopen error>")

    monkeypatch.setattr(client, "power_state", unreadable)
    with pytest.raises(ProxmoxError) as failure:
        client._reconcile_power_action(
            "mng01",
            40000,
            "demo",
            "start",
            ProxmoxError("respuesta perdida", uncertain=True),
        )
    assert failure.value.uncertain is True


def test_power_start_reintenta_una_vez_tras_tarea_fallida_y_vm_detenida(monkeypatch):
    client = object.__new__(ProxmoxClient)
    error = ProxmoxError("POWER_TASK_FAILED: unable to open file", uncertain=False)
    error.task = "UPID:mng01:00000001:00000002:00000003:qmstart:40000:"
    starts = []

    monkeypatch.setattr(
        client,
        "power_state",
        lambda *_args: {"power_state": "stopped", "name": "demo"},
    )
    monkeypatch.setattr(
        client,
        "_submit_power_action",
        lambda _node, _vmid, action: (
            starts.append(action)
            or {"action": action, "power_state": "running", "exitstatus": "OK"}
        ),
    )

    result = client._reconcile_power_action("mng01", 40000, "demo", "start", error)

    assert starts == ["start"]
    assert result["retry_after_failed_task"] is True
    assert result["recovered"] is True
    assert result["power_state"] == "running"


def test_power_start_no_repite_una_tarea_con_respuesta_incierta(monkeypatch):
    client = object.__new__(ProxmoxClient)
    error = ProxmoxError("POWER_PROXMOX_UNAVAILABLE", uncertain=True)
    error.task = "UPID:mng01:00000001:00000002:00000003:qmstart:40000:"
    monkeypatch.setattr(
        client,
        "power_state",
        lambda *_args: {"power_state": "stopped", "name": "demo"},
    )

    def wait_task(*_args):
        raise ProxmoxError("POWER_TASK_UNCERTAIN", uncertain=True)

    monkeypatch.setattr(client, "wait_task", wait_task)
    monkeypatch.setattr(
        client,
        "_submit_power_action",
        lambda *_args: pytest.fail("no se debe duplicar una orden de resultado incierto"),
    )

    with pytest.raises(ProxmoxError, match="POWER_OPERATION_UNCERTAIN") as failure:
        client._reconcile_power_action("mng01", 40000, "demo", "start", error)

    assert failure.value.uncertain is True
