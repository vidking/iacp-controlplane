import hmac
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import secret

bearer = HTTPBearer(auto_error=False, description="Token compartido de laboratorio; sin aislamiento por tenant.")


def authorize(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    try:
        expected = secret("IACP_API_TOKEN_FILE")
    except (KeyError, OSError, UnicodeError):
        raise HTTPException(status_code=503, detail="Authentication unavailable") from None
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Authentication unavailable")
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode("utf8"), expected.encode("utf8")
    ):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
