from ..services.jobs import Publisher


def get_smoke_publisher() -> Publisher:
    from ..worker import smoke

    return smoke.delay
