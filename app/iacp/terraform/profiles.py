"""Server-authorized machine profiles. DHCP never opens the static IPAM file."""
import json
import os
from pathlib import Path
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select

from .config import Allocation, MachineSettings, PlanError, StrictModel


class Profile(StrictModel):
    allowed_tenants: list[str] = Field(min_length=1)
    settings: MachineSettings
    vmid_min: int = Field(ge=30000, le=999999999)
    vmid_max: int = Field(ge=30000, le=999999999)
    max_live_instances: int = Field(default=1, ge=1, le=1000)
    max_vcpus: int = Field(default=8, ge=1, le=64)
    max_memory_mb: int = Field(default=8192, ge=512)
    max_disk_gb: int = Field(default=100, ge=8)

    @model_validator(mode='after')
    def valid_policy(self):
        for tenant in self.allowed_tenants:
            UUID(tenant)
        if self.vmid_max < self.vmid_min or self.vmid_max-self.vmid_min > 10000:
            raise ValueError('Bounded VMID range required')
        return self


def load_profile(name, tenant_id):
    try:
        data=json.loads(Path(os.environ['IACP_TERRAFORM_PROFILES_FILE']).read_text())
        profile=Profile.model_validate(data['profiles'][name])
        if tenant_id not in profile.allowed_tenants:
            raise ValueError('Profile not authorized')
        return profile
    except (KeyError, OSError, ValueError, TypeError):
        raise PlanError('AUTHORIZED_PROFILE_REQUIRED') from None


def reserve_profile(session, instance):
    """Caller holds the existing profile advisory lock through commit/rollback."""
    from ..models import Instance, TerraformExecution
    p=load_profile(instance.network_profile,instance.tenant_id)
    if instance.image != p.settings.image:
        raise PlanError('AUTHORIZED_IMAGE_MISMATCH')
    if instance.vcpus>p.max_vcpus or instance.memory_mb>p.max_memory_mb or instance.disk_gb>p.max_disk_gb:
        raise PlanError('PROFILE_CAPACITY_EXCEEDED')
    instance.network_mode=p.settings.network_mode
    if instance.network_mode=='static':
        return None  # Existing per-instance IPAM is resolved ONLY by the worker.
    live=session.scalar(select(func.count()).select_from(Instance).where(
        Instance.network_profile==instance.network_profile,Instance.status!='deleted'))
    if live >= p.max_live_instances:
        raise PlanError('PROFILE_CAPACITY_EXCEEDED')
    used=set(session.scalars(select(TerraformExecution.vmid).where(TerraformExecution.vmid.is_not(None))))
    used.update(cluster_vmids())
    vmid=next((n for n in range(p.vmid_min,p.vmid_max+1) if n not in used and n!=p.settings.template_vm_id),None)
    if vmid is None:
        raise PlanError('PROFILE_VMID_EXHAUSTED')
    # A machine identity reservation, never an IPAM address allocation.
    return Allocation(vm_id=vmid,**p.settings.model_dump())


def cluster_vmids():
    from ..lifecycle.proxmox import ProxmoxPower,PowerError
    try:
        client=ProxmoxPower.configured()
        permissions=client._request('GET','/access/permissions?path=/vms')
        if permissions.get('/vms',{}).get('VM.Audit')!=1:
            raise PlanError('PROFILE_INVENTORY_SCOPE_REQUIRED')
        rows=client._request('GET','/cluster/resources?type=vm')
        return {int(row['vmid']) for row in rows}
    except (PowerError,ValueError,TypeError,KeyError):
        raise PlanError('PROFILE_INVENTORY_UNAVAILABLE') from None
