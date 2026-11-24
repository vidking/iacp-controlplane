"""Migración aditiva, sin alterar el historial de operaciones KVM."""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS service_instances (
        service_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL UNIQUE REFERENCES instances(instance_id),
        application_id TEXT NOT NULL, recipe_version TEXT NOT NULL,
        configuration_json TEXT NOT NULL, secrets_encrypted TEXT,
        status TEXT NOT NULL, credentials_retrieved_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS service_jobs (
        job_id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES service_instances(service_id),
        action TEXT NOT NULL DEFAULT 'install', status TEXT NOT NULL,
        phase TEXT NOT NULL DEFAULT 'waiting_vm', worker_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
        last_heartbeat TEXT, error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, finished_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS service_credential_retrievals (
        service_id TEXT PRIMARY KEY REFERENCES service_instances(service_id) ON DELETE CASCADE,
        request_hash TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_service_active ON service_jobs(service_id)
        WHERE status IN ('queued', 'running', 'uncertain')""",
    """CREATE TABLE IF NOT EXISTS service_events (
        event_id BIGSERIAL PRIMARY KEY, job_id TEXT NOT NULL REFERENCES service_jobs(job_id),
        phase TEXT NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_service_events_job_id_event_id ON service_events(job_id, event_id)",
    """CREATE TABLE IF NOT EXISTS service_workers (
        worker_id TEXT PRIMARY KEY, status TEXT NOT NULL, job_id TEXT, last_heartbeat TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS service_releases (
        service_id TEXT PRIMARY KEY REFERENCES service_instances(service_id), digest TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS service_bindings (
        service_id TEXT PRIMARY KEY REFERENCES service_instances(service_id),
        binding_json TEXT NOT NULL, observed_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_service_jobs_status ON service_jobs(status, created_at)",
)
