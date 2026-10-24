from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar('iacp_correlation_id', default=None)
