#!/usr/bin/env python3
"""
Drop-in replacement for `airflow standalone` that respects the configured
auth_manager (e.g. FabAuthManager for LDAP) instead of forcing
SimpleAuthManager.

Upstream `airflow standalone` overrides AIRFLOW__CORE__AUTH_MANAGER on every
launch, regardless of what's in airflow.cfg, which makes LDAP login
impossible from the standalone command. This script reuses everything else
about StandaloneCommand (subprocess management, colored output, ready
detection, signal handling) and just skips the auth_manager override.

Usage:
    source ./venv.sh
    export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/airflow-standalone-ldap.py
"""

import os
import sys

# Standalone uses these
from airflow.cli.commands.standalone_command import StandaloneCommand
from airflow.executors.executor_loader import ExecutorLoader
from airflow.executors import executor_constants


class HammerStandalone(StandaloneCommand):
    """StandaloneCommand without the SimpleAuthManager-specific behaviour."""

    def calculate_env(self):
        env = dict(os.environ)

        # Keep the LocalExecutor override (standalone is single-machine by design).
        executor_class, _ = ExecutorLoader.import_default_executor_cls()
        if not executor_class.is_local:
            self.print_output("standalone", "Forcing executor to LocalExecutor")
            env["AIRFLOW__CORE__EXECUTOR"] = executor_constants.LOCAL_EXECUTOR

        # Deliberately DO NOT override AIRFLOW__CORE__AUTH_MANAGER.
        # Whatever's in airflow.cfg (FabAuthManager, SimpleAuthManager, ...) wins.
        self.print_output("standalone", "Respecting configured auth_manager: not forcing SimpleAuthManager")
        return env

    def find_user_info(self):
        """
        Upstream's find_user_info() tries to print the SimpleAuthManager's
        auto-generated admin password. FabAuthManager has no such file (users
        come from LDAP), so we just print a hint and return.
        """
        self.print_output(
            "standalone",
            "Auth manager is not SimpleAuthManager: skipping admin-password lookup. "
            "Log in via your configured auth backend (e.g. LDAP).",
        )


def main():
    HammerStandalone().run()


if __name__ == "__main__":
    sys.exit(main())
