"""Airflow cluster policy: completion-email notifications for every DAG.

Airflow imports this file once at startup (``settings.import_local_settings``,
from ``$AIRFLOW_HOME/config`` on ``sys.path``) and then calls ``dag_policy`` for
every DAG as it loads. Setting the callbacks here means every DAG -- current and
future -- notifies its triggering user when it finishes, without wiring the
callback into each ``@dag`` decorator by hand.

The callback lives in ``hammer.vlsi.pd_notify``, which has no Airflow imports, so
importing it here does not construct any DAGs during settings initialisation
(importing ``hammer_vlsi`` would, since it builds its demo DAGs at import time).

Applying this to every DAG is safe because the callback decides per user and per
DAG whether to actually send: it only emails when the triggering user has both
registered an address and toggled that specific DAG on (see the user_notify_email
and user_notify_dag tables and the "My Notification Email" page). A DAG nobody
opted into simply sends nothing.

We assign the callback unconditionally. The few demo DAGs that also set it inline
end up with the same single function, so there is no double send; every other DAG
gains it. Activating a change here requires an Airflow restart, since local
settings are imported only at process startup.
"""

from hammer.vlsi.pd_notify import notify_flow_complete


def dag_policy(dag):
    """Email the triggering user when any DAG run finishes (success or failure)."""
    dag.on_success_callback = notify_flow_complete
    dag.on_failure_callback = notify_flow_complete
