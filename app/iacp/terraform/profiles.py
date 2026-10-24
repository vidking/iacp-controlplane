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
    target_nodes: list[str] | None = Field(default=None, min_length=1)
    self_service_enabled: bool = False
    max_total_vcpus: int = Field(default=8, ge=1, le=4096)
    max_total_memory_mb: int = Field(default=8192, ge=512, le=1048576)
    max_total_disk_gb: int = Field(default=100, ge=8, le=1048576)
    max_concurrent_operations: int = Field(default=1, ge=1, le=32)

    @model_validator(mode='after')
    def valid_policy(self):
        for tenant in self.allowed_tenants:
            UUID(tenant)
        if self.vmid_max < self.vmid_min or self.vmid_max-self.vmid_min > 10000:
            raise ValueError('Bounded VMID range required')
        if self.target_nodes and (len(set(self.target_nodes)) != len(self.target_nodes)
                or not set(self.target_nodes) <= set(self.settings.allowed_nodes)):
            raise ValueError('Explicit authorized target nodes required')
        return self


def settings_for(profile, node):
    if node not in (profile.target_nodes or [profile.settings.node_name]):
        raise PlanError('PROFILE_NODE_NOT_AUTHORIZED')
    updates={'node_name':node}
    if node!=profile.settings.node_name and profile.settings.template_node_name is None:
        updates['template_node_name']=profile.settings.node_name
    return profile.settings.model_copy(update=updates)


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
    if instance.disk_gb<p.settings.boot_disk.minimum_size_gb:
        raise PlanError('DISK_SHRINK_FORBIDDEN')
    instance.network_mode=p.settings.network_mode
    if instance.network_mode=='static':
        if instance.node_preference:
            raise PlanError('STATIC_IPAM_NODE_FIXED')
        return None  # Existing per-instance IPAM is resolved ONLY by the worker.
    rows=session.scalars(select(Instance).where(Instance.tenant_id==instance.tenant_id,
        Instance.network_profile==instance.network_profile,Instance.status!='deleted')).all()
    if (len(rows) >= p.max_live_instances or sum(i.vcpus for i in rows)+instance.vcpus > p.max_total_vcpus
            or sum(i.memory_mb for i in rows)+instance.memory_mb > p.max_total_memory_mb
            or sum(i.disk_gb for i in rows)+instance.disk_gb > p.max_total_disk_gb):
        raise PlanError('PROFILE_CAPACITY_EXCEEDED')
    nodes=p.target_nodes or [p.settings.node_name]
    allocations=session.scalars(select(TerraformExecution.allocation).join(Instance).where(Instance.status!='deleted')).all()
    node=instance.node_preference or min(nodes,key=lambda n:(sum(a.get('node_name')==n for a in allocations if a),n))
    settings=settings_for(p,node)
    used=set(session.scalars(select(TerraformExecution.vmid).where(TerraformExecution.vmid.is_not(None))))
    used.update(cluster_vmids())
    vmid=next((n for n in range(p.vmid_min,p.vmid_max+1) if n not in used and n!=p.settings.template_vm_id),None)
    if vmid is None:
        raise PlanError('PROFILE_VMID_EXHAUSTED')
    # A machine identity reservation, never an IPAM address allocation.
    return Allocation(vm_id=vmid,**settings.model_dump())


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
