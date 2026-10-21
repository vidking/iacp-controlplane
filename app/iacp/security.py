from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
from sqlalchemy.orm import Session

from .config import secret
from .db import get_session
from .models import BOOTSTRAP_TENANT_ID, Tenant

bearer = HTTPBearer(auto_error=False, description="Token ligado en el servidor a un tenant y un rol.")


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
    role: Literal["operator", "reader"]


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
                if hmac.compare_digest(digest, entry.token_sha256):
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
