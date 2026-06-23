"""Airflow cluster policy: wire completion-email notifications into every DAG.

Airflow imports this once at startup (from $AIRFLOW_HOME/config) and runs
dag_policy for each DAG as it loads, so every DAG -- current and future -- gets
the callback and the per-run notify toggle without touching each @dag decorator.
The callback lives in pd_notify, which has no Airflow imports, so pulling it in
here doesn't build any DAGs while settings load. Changes take effect on restart.
"""

from hammer.vlsi.pd_notify import notify_flow_complete


def dag_policy(dag):
    """Add the completion callback and the "Email me when this finishes" toggle.

    Setting the callback on every DAG is safe: it only emails when the run's
    toggle is on and the user has a registered address, so a DAG nobody opted
    into sends nothing.
    """
    dag.on_success_callback = notify_flow_complete
    dag.on_failure_callback = notify_flow_complete
    try:
        if "notify" not in dag.params:
            from airflow.sdk import Param
            dag.params["notify"] = Param(
                True,
                type="boolean",
                title="Email me when this finishes",
                description="Send a note to your registered address when this run completes.",
            )
    except Exception:
        pass
