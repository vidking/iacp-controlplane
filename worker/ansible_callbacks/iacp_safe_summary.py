"""Callback de stdout que emite solo códigos allowlist, nunca resultados Ansible."""

from __future__ import annotations

import sys
from pathlib import Path

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from worker.service_diagnostics import task_code

CALLBACK_VERSION = 2.0
CALLBACK_TYPE = "stdout"
CALLBACK_NAME = "iacp_safe_summary"
CALLBACK_NEEDS_WHITELIST = False


class CallbackModule(CallbackBase):
    """Emite únicamente el código seguro de la tarea en curso o fallida."""

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._display.display(f"IACP_TASK:{task_code(task.get_name())}")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._display.display(
            f"IACP_FAILED:{task_code(result._task.get_name())}"
        )

    def v2_runner_on_unreachable(self, result):
        self._display.display(
            f"IACP_UNREACHABLE:{task_code(result._task.get_name())}"
        )
