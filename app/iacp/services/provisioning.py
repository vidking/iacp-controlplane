"""Small HTTP-facing boundary; the existing implementation owns all fences/state.

This is intentionally one implementation, with no engine registry or migration.
"""
from ..terraform.actions import cancel, retry
from ..terraform.apply import approve, cancel_apply, dispatch, enabled, recover_apply, resolve_absence
from ..terraform import deletion
from ..terraform.config import PlanError as ProvisioningError


class ProvisioningService:
    cancel_plan = staticmethod(cancel)
    retry_plan = staticmethod(retry)
    approve = staticmethod(approve)
    dispatch = staticmethod(dispatch)
    cancel_execution = staticmethod(cancel_apply)
    recover_execution = staticmethod(recover_apply)
    resolve_absence = staticmethod(resolve_absence)
    execution_enabled = staticmethod(enabled)
    deletion = deletion
