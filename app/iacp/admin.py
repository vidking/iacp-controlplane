"""Alta explícita de tenants por el operador, sin credenciales en la base."""
import argparse
import json
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_session_factory
from .models import Tenant


class TenantInput(BaseModel):
    slug: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=128)


def create_tenant(session: Session, spec: TenantInput) -> Tenant:
    tenant = Tenant(id=str(uuid4()), **spec.model_dump())
    session.add(tenant)
    session.commit()
    return tenant


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create-tenant"])
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    with get_session_factory()() as session:
        tenant = create_tenant(session, TenantInput(slug=args.slug, name=args.name))
        print(json.dumps({"tenant_id": tenant.id, "slug": tenant.slug}))
