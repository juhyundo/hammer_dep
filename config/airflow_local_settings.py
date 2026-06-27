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
    # Airflow 3.1.0 derives has_on_*_callback once, when the DAG is built, from
    # whether the @dag decorator set a callback. We attach ours here, after the
    # DAG is built, so those flags stay False and serialize that way -- and the
    # scheduler only enqueues a completion callback when the flag is True, so no
    # email ever fires for a policy-only DAG. Refresh the flags to match.
    dag.has_on_success_callback = True
    dag.has_on_failure_callback = True
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
