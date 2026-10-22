"""Test-only ownership promotion; never copied into application images."""
from iacp.lifecycle.worker import celery_app
from iacp.lifecycle import ownership

ownership.KVM_POWER_SOURCE_REVISION = "e" * 40
ownership.config.SOURCE_REVISION = "e" * 40
