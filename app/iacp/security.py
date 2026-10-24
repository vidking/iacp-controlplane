from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
from sqlalchemy.orm import Session

from .config import secret
from .db import get_session
from .models import BOOTSTRAP_TENANT_ID, Tenant

bearer = HTTPBearer(auto_error=False, description="Token ligado en el servidor a un tenant y un rol.")
docs_basic = HTTPBasic(auto_error=False)


def request_uses_https(request) -> bool:
    """TLS termination is trusted only from explicitly configured proxy IPs.

    Runtime disables uvicorn proxy headers. Never trust a caller's forwarded
    header without checking the socket peer; no trust-all default.
    """
    if request.url.scheme == 'https':
        return True
    from ipaddress import ip_address
    try:
        peers = [ip_address(value.strip()) for value in os.environ.get('IACP_TLS_PROXY_IPS', '').split(',') if value.strip()]
        return bool(request.client and ip_address(request.client.host) in peers
            and request.headers.get('x-iacp-transport') == 'https')
    except ValueError:
        return False


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    role: str


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tenant_id: UUID
    subject: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:@-]+$")
    role: Literal["operator", "reader", "approver", "docs"]


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[Session, Depends(get_session)],
) -> Principal:
    try:
        entries = []
        registry = os.environ.get("IACP_TENANT_CREDENTIALS_FILE")
        if registry:
            raw = json.loads(Path(registry).read_text(encoding="utf8"))
            if not isinstance(raw, list) or not raw:
                raise ValueError("Invalid credential registry")
            entries = [Credential.model_validate(item) for item in raw]
            if len({item.token_sha256 for item in entries}) != len(entries):
                raise ValueError("Duplicate credential")
        expected = secret("IACP_API_TOKEN_FILE") if "IACP_API_TOKEN_FILE" in os.environ else None
        if (expected is not None and len(expected) < 32) or (expected is None and not entries):
            raise ValueError("Invalid authentication configuration")
        if expected and any(hmac.compare_digest(hashlib.sha256(expected.encode()).hexdigest(), item.token_sha256) for item in entries):
            raise ValueError("Bootstrap credential cannot be remapped")
    except (KeyError, OSError, UnicodeError, ValueError, ValidationError):
        raise HTTPException(status_code=503, detail="Authentication unavailable") from None
    principal = None
    if credentials is not None:
        supplied = credentials.credentials.encode("utf8")
        if expected and hmac.compare_digest(supplied, expected.encode("utf8")):
            principal = Principal(BOOTSTRAP_TENANT_ID, "bootstrap", "operator")
        if len(supplied) >= 32:
            digest = hashlib.sha256(supplied).hexdigest()
            for entry in entries:
                if entry.role != "docs" and hmac.compare_digest(digest, entry.token_sha256):
                    principal = Principal(str(entry.tenant_id), entry.subject, entry.role)
    if principal is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    tenant = session.get(Tenant, principal.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=403, detail="Tenant unavailable")
    return principal


def authorize_write(principal: Annotated[Principal, Depends(authorize)]) -> Principal:
    if principal.role != "operator":
        raise HTTPException(status_code=403, detail="Write permission required")
    return principal


def authorize_approval(principal: Annotated[Principal, Depends(authorize)]) -> Principal:
    if principal.role != "approver":
        raise HTTPException(status_code=403, detail="Approval permission required")
    return principal


def authorize_docs(
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    basic: Annotated[HTTPBasicCredentials | None, Depends(docs_basic)],
) -> Principal | None:
    """Browser challenge uses the existing tenant token; no cookies or new identity store."""
    if basic is not None:
        try:
            registry = os.environ.get("IACP_TENANT_CREDENTIALS_FILE")
            entries = [Credential.model_validate(item) for item in json.loads(Path(registry).read_text())] if registry else []
            docs = [entry for entry in entries if entry.role == "docs"]
            if len({entry.token_sha256 for entry in entries}) != len(entries) or len({entry.subject for entry in docs}) != len(docs):
                raise ValueError("Ambiguous credential registry")
            # Once explicit docs identities exist, Basic no longer accepts API
            # tokens. Bearer automation retains its existing tenant/RBAC checks.
            if docs:
                digest = hashlib.sha256(basic.password.encode()).hexdigest()
                entry = next((entry for entry in docs if hmac.compare_digest(entry.subject, basic.username)
                              and hmac.compare_digest(entry.token_sha256, digest)), None)
                if entry is None:
                    raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="IACP docs"'})
                tenant = session.get(Tenant, str(entry.tenant_id))
                if tenant is None or not tenant.active:
                    raise HTTPException(403, "Tenant unavailable")
                return None
        except (OSError, ValueError, TypeError, ValidationError):
            raise HTTPException(503, "Documentation authentication unavailable") from None
        if basic.username == "iacp":
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=basic.password)
    try:
        return authorize(credentials, session)
    except HTTPException as error:
        if error.status_code == 401:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="IACP docs", charset="UTF-8"'}) from None
        raise
