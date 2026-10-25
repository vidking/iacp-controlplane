from ..services.jobs import Publisher


def get_smoke_publisher() -> Publisher:
    from ..worker import smoke

    return smoke.delay


def get_instance_publisher() -> Publisher:
    from ..worker import provision_instance
    from ..terraform.config import instance_executor

    if instance_executor() == "terraform":
        return lambda job_id: get_plan_publisher()(job_id, 1)

    return provision_instance.delay


def get_plan_publisher():
    from ..terraform.worker import plan_instance

    return lambda job_id, attempt: plan_instance.apply_async(args=[job_id, attempt], queue="terraform")


def get_apply_publisher():
    from ..terraform.worker import apply_instance
    return lambda identifier, mode: apply_instance.apply_async(args=[identifier, mode], queue="terraform")


def get_power_publisher():
    from ..lifecycle.worker import power_operation
    return lambda identifier: power_operation.apply_async(args=[identifier], queue="lifecycle")


def get_deletion_publisher():
    from ..terraform.worker import delete_instance
    return lambda identifier, mode: delete_instance.apply_async(args=[identifier, mode], queue="terraform")


def get_network_publisher():
    from ..terraform.worker import discover_network
    return lambda identifier: discover_network.apply_async(args=[identifier],queue='terraform')


def get_guest_initialization_publisher():
    from ..terraform.worker import initialize_guest
    return lambda identifier: initialize_guest.apply_async(args=[identifier],queue='terraform')


def get_access_verification_publisher():
    from ..terraform.worker import verify_access
    return lambda identifier: verify_access.apply_async(args=[identifier], queue='terraform')


def get_autostart_publisher():
    from ..lifecycle.worker import continue_instance_start
    return lambda identifier: continue_instance_start.apply_async(args=[identifier], queue='lifecycle')


def get_commercial_bootstrap_publisher():
    from ..terraform.worker import bootstrap_root
    return lambda identifier: bootstrap_root.apply_async(args=[identifier],queue='terraform')


def get_power_client():
    from ..lifecycle.proxmox import ProxmoxPower
    return ProxmoxPower.configured
