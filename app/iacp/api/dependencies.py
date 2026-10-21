from ..services.jobs import Publisher


def get_smoke_publisher() -> Publisher:
    from ..worker import smoke

    return smoke.delay


def get_instance_publisher() -> Publisher:
    from ..worker import provision_instance

    return provision_instance.delay
