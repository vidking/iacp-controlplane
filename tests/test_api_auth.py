import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.main import authorize


def _request_from(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/environments",
            "query_string": b"",
            "headers": [],
            "client": (host, 12345),
            "server": ("localhost", 8080),
        }
    )


@pytest.mark.no_database
def test_insecure_development_auth_allows_only_testclient(monkeypatch):
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "develop")
    monkeypatch.setenv("IACP_ALLOW_INSECURE_LOCAL_DEV", "1")
    monkeypatch.setenv("IACP_API_TOKEN", "")

    authorize(None, _request_from("testclient"))


@pytest.mark.no_database
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "198.51.100.10"])
def test_insecure_development_auth_rejects_network_peer(monkeypatch, host):
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "develop")
    monkeypatch.setenv("IACP_ALLOW_INSECURE_LOCAL_DEV", "1")
    monkeypatch.setenv("IACP_API_TOKEN", "")

    with pytest.raises(HTTPException) as error:
        authorize(None, _request_from(host))

    assert error.value.status_code == 503
