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
