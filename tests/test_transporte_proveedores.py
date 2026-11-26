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
