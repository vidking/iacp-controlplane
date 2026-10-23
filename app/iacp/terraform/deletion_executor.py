"""Saved destroy plans for one explicitly disposable KVM. No direct PVE mutation."""
import io
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tarfile
from uuid import UUID
from urllib.request import build_opener, HTTPSHandler, ProxyHandler

from ..lifecycle.proxmox import NoRedirect, PowerError, ProxmoxPower, Target, granted
from .config import PlanError, SOURCE_REVISION
from .executor import (ADDRESS, TerraformExecutor, digest_file, managed_resources,
                       state_resource, verify_identity, verify_plan, verify_postconditions, write_private)
from .observation import proxmox_resource
from .state import S3StateReader


def verify_delete_plan(plan, variables):
    if variables['instance']['vm_id'] < 30000 or variables['instance']['vm_id'] in {104, 106}:
        raise PlanError('DELETE_UNSAFE_PLAN')
    if (plan.get('errored') is not False or plan.get('complete') is not True
            or plan.get('deferred_changes') or plan.get('action_invocations')
            or plan.get('deferred_action_invocations')):
        raise PlanError('DELETE_UNSAFE_PLAN')
    changes = plan.get('resource_changes', [])
    if len(changes) != 1:
        raise PlanError('DELETE_UNSAFE_PLAN')
    change = changes[0]
    detail = change.get('change', {})
    if (change.get('address') != ADDRESS or change.get('mode') != 'managed'
            or change.get('provider_name') != 'registry.terraform.io/bpg/proxmox'
            or detail.get('actions') != ['delete'] or detail.get('after') is not None
            or detail.get('importing') or change.get('deposed') or change.get('index') is not None):
        raise PlanError('DELETE_UNSAFE_PLAN')
    verify_identity(detail.get('before') or {}, variables['instance'])
    before = managed_resources(plan.get('prior_state', {}).get('values', {}))
    if len(before) != 1 or before[0].get('address') != ADDRESS or managed_resources(plan.get('planned_values', {})):
        raise PlanError('DELETE_UNSAFE_PLAN')
    verify_identity(before[0]['values'], variables['instance'])


def stopped(env, variables, cancelled):
    """Read-only checks with the Terraform identity; no Lifecycle credential mount."""
    if cancelled():
        raise PlanError('CANCELLED')

    expected = variables['instance']
    target = Target(expected['vm_id'], expected['node_name'], expected['vm_name'])
    context = ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))
    client = ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'), env['PROXMOX_VE_API_TOKEN'],
                          build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect()))
    try:
        privileges = client._request('GET', '/access/permissions?path=/vms/' + str(target.vmid))
        privileges = privileges.get('/vms/' + str(target.vmid), {})
        if not all(granted(privileges, key) for key in ('VM.Audit', 'VM.Allocate')):
            raise PlanError('DELETE_PERMISSIONS_REQUIRED')
        if client.observe(target, preflight=True)['power_state'] != 'stopped':
            raise PlanError('DELETE_STOPPED_KVM_REQUIRED')
    except PowerError as error:
        raise PlanError(error.code) from None
    if cancelled():
        raise PlanError('CANCELLED')

def verify_topology(approved, current, *, persisted=None):
    """Retain provisioning checks; only started belongs to Lifecycle after create.

    Extra disks/NICs must never be swept into a deletion after an external attach.
    The sealed creation plan and its provisioning postconditions are not changed.
    """
    checked = deepcopy(approved)
    change = checked['resource_changes'][0]['change']
    for field in ('disk', 'network_device'):
        expected = change['after'].get(field)
        actual = current.get(field)
        if not isinstance(expected, list) or not isinstance(actual, list) or len(expected) != len(actual):
            raise PlanError('DELETE_TOPOLOGY_CHANGED')
    unknown = change.setdefault('after_unknown', {})
    if not isinstance(unknown, dict):
        raise PlanError('DELETE_UNSAFE_PLAN')
    unknown['started'] = True
    verify_postconditions(checked, current, source='delete.topology', persisted=persisted)


class DeletionExecutor(TerraformExecutor):
    def deletion_workspace(self, instance_id, operation_id):
        return self.artifacts / str(UUID(instance_id)) / ('deletion-' + str(UUID(operation_id)))

    def manifest(self, context):
        workspace = self.deletion_workspace(context['instance_id'], context['operation_id'])
        try:
            manifest = json.loads((workspace / 'deletion-manifest.json').read_text())
            if (manifest['context'] != context or manifest['source_revision'] != SOURCE_REVISION
                    or digest_file(workspace / 'source/stacks/kvm-instance/deletion.tfplan') != manifest['plan_sha256']):
                raise ValueError()
            root = workspace / 'source/stacks/kvm-instance'
            if (json.loads((root / 'iacp.auto.tfvars.json').read_text()) != context['variables']
                    or json.loads((root / 'backend.tfbackend.json').read_text()) != context['backend']):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise PlanError('DELETE_PLAN_INTEGRITY_FAILED') from None
        return workspace, manifest

    def provisioning_plan(self, context, root, env, cancelled):
        self.verify_artifacts(context['instance_id'], context['attempt'], context['provisioning_plan_sha256'],
                              context['state_key'], context['allocation'], context['variables'])
        saved = self.workspace(context['instance_id'], context['attempt']) / 'source/stacks/kvm-instance/saved.tfplan'
        plan = self.read_json(['show', '-json', str(saved)], root, env, cancelled, 'delete.provisioning_plan')
        verify_plan(plan, context['variables'])
        return plan

    def plan_deletion(self, context, cancelled):
        # The original immutable provisioning plan must still belong to this instance.
        self.verify_artifacts(context['instance_id'], context['attempt'], context['provisioning_plan_sha256'],
                              context['state_key'], context['allocation'], context['variables'])
        workspace = self.deletion_workspace(context['instance_id'], context['operation_id'])
        workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
        archive = subprocess.run(['git', '-c', f'safe.directory={self.source}', '-C', str(self.source),
                                  'archive', SOURCE_REVISION], capture_output=True, check=True, timeout=30).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            if any(x.issym() or x.islnk() for x in source.getmembers()):
                raise PlanError('INVALID_SOURCE')
            source.extractall(workspace / 'source', filter='data')
        root = workspace / 'source/stacks/kvm-instance'
        write_private(root / 'iacp.auto.tfvars.json', context['variables'])
        write_private(root / 'backend.tfbackend.json', context['backend'])
        env = self.environment(workspace)
        reader = S3StateReader(context['backend'], env)
        try:
            state = reader.read(cancelled)
            resource = state_resource(state.state, context['variables'])
            if resource is None:
                raise PlanError('DELETE_OWNED_STATE_REQUIRED')
            stopped(env, context['variables'], cancelled)
            self.initialize(root, env, cancelled, 'delete.plan')
            approved = self.provisioning_plan(context, root, env, cancelled)
            verify_topology(approved, resource)
            self.runner.plan_destroy(root, env, cancelled)
            plan = self.read_json(['show', '-json', 'deletion.tfplan'], root, env, cancelled, 'delete.inspect')
            verify_delete_plan(plan, context['variables'])
            verify_topology(approved, plan['resource_changes'][0]['change']['before'], persisted=resource)
            state.verify(reader.read(cancelled))
            stopped(env, context['variables'], cancelled)
            saved = root / 'deletion.tfplan'; saved.chmod(0o600)
            salt = secrets.token_hex(32)
            manifest = {'context': context, 'source_revision': SOURCE_REVISION, 'plan_sha256': digest_file(saved),
                        'state_before': state.metadata(), 'execution_context_salt': salt,
                        'execution_context_sha256': self.context_digest(env, salt)}
            write_private(workspace / 'deletion-manifest.json', manifest)
            return {'plan_sha256': manifest['plan_sha256'], 'state_before': manifest['state_before']}
        finally:
            reader.close()

    def apply_deletion(self, context, sha256, cancelled, before_mutation):
        workspace, manifest = self.manifest(context)
        env = self.environment(workspace)
        if (manifest['plan_sha256'] != sha256 or self.context_digest(env, manifest['execution_context_salt'])
                != manifest['execution_context_sha256']):
            raise PlanError('DELETE_PLAN_INTEGRITY_FAILED')
        reader = S3StateReader(context['backend'], env)
        try:
            current = reader.read(cancelled)
            if current.metadata() != manifest['state_before'] or state_resource(current.state, context['variables']) is None:
                raise PlanError('DELETE_STATE_CHANGED')
            stopped(env, context['variables'], cancelled)
            root = workspace / 'source/stacks/kvm-instance'
            self.initialize(root, env, cancelled, 'delete.apply')
            approved = self.provisioning_plan(context, root, env, cancelled)
            verify_topology(approved, state_resource(current.state, context['variables']))
            plan = self.read_json(['show', '-json', 'deletion.tfplan'], root, env, cancelled, 'delete.reinspect')
            verify_delete_plan(plan, context['variables'])
            verify_topology(approved, plan['resource_changes'][0]['change']['before'],
                            persisted=state_resource(current.state, context['variables']))
            current.verify(reader.read(cancelled))
            stopped(env, context['variables'], cancelled)
            if digest_file(root / 'deletion.tfplan') != sha256:
                raise PlanError('DELETE_PLAN_INTEGRITY_FAILED')
            before_mutation()  # Durable consumption fence BEFORE spawning Terraform.
            self.runner.apply_destroy(root, env, cancelled)
        finally:
            reader.close()
        return self.reconcile_deletion(context, sha256, cancelled)

    def reconcile_deletion(self, context, sha256, cancelled):
        # No init, plan, apply, import, state mutation or native PVE POST on recovery.
        workspace, manifest = self.manifest(context)
        if manifest['plan_sha256'] != sha256:
            raise PlanError('DELETE_PLAN_INTEGRITY_FAILED')
        env = self.environment(workspace)
        reader = S3StateReader(context['backend'], env)
        try:
            state = reader.read(cancelled)
            baseline = manifest['state_before']
            if state.state['lineage'] != baseline['lineage'] or state.state['serial'] <= baseline['serial']:
                raise PlanError('DELETE_ABSENCE_UNCONFIRMED')
            resource = state_resource(state.state, context['variables'])
            observed = proxmox_resource(env, context['variables']['instance'], cancelled)
            consistency = state.verify(reader.read(cancelled))
            if resource is not None or observed['present']:
                raise PlanError('DELETE_ABSENCE_UNCONFIRMED')
            return {'deleted': True, 'state_present': False, 'proxmox_present': False,
                    'state_consistency': consistency, 'vmid': context['variables']['instance']['vm_id']}
        finally:
            reader.close()
