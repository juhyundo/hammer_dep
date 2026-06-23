"""Flow-completion email notifications.

This lives in its own module, with no Airflow imports, so importing it never
constructs DAGs. That lets both the DAGs in hammer_vlsi.py and the cluster
policy in config/airflow_local_settings.py attach the same callback without the
policy accidentally building DAGs during Airflow's settings initialisation.

Airflow 3 runs DAG callbacks in a sandbox with no metadata-DB access and no
triggering user in the context, so the resolve path reads the dag_run row over
the SLEDGE_ connection channel (see pd_store.airflow_metadata_conn_settings).
"""

import os

from hammer.vlsi import pd_store


def _resolve_notify_email(context):
    """Return the address to notify about a finished run, or None to stay quiet.

    Two opt-ins must both hold: the user registered an address (one per person,
    in user_notify_email) AND toggled this specific DAG on (user_notify_dag).
    We never guess an address from the username, and there is deliberately no
    per-run recipient override -- that would let anyone who can trigger a DAG
    send mail to an arbitrary address.
    """
    dag_run = context.get("dag_run") if isinstance(context, dict) else getattr(context, "dag_run", None)
    dag_id = getattr(dag_run, "dag_id", None)
    run_id = getattr(dag_run, "run_id", None)
    # Resolve the triggering user. The runtime proxy hides triggering_user_name
    # and the callback sandbox forbids ORM access, so fall back to a direct read
    # of the dag_run row keyed by (dag_id, run_id) over the SLEDGE_ channel.
    uid = getattr(dag_run, "triggering_user_name", None)
    if not uid:
        uid = pd_store.lookup_triggering_user(dag_id, run_id)
    if not uid or uid == "default":
        print(f"[notify] resolve: no triggering user for {dag_id} {run_id}")
        return None
    # Per-DAG opt-in: only notify if this user toggled THIS dag on. Independent
    # per dag -- enabling one never enables another.
    try:
        if not pd_store.is_dag_notify_enabled(uid, dag_id):
            print(f"[notify] resolve: {uid} has notifications off for {dag_id}")
            return None
    except Exception as e:
        print(f"[notify] resolve: dag-enable check failed for {uid}/{dag_id}: {e}")
        return None
    # The address itself -- one per user, shared across all their enabled DAGs.
    try:
        addr = pd_store.get_notify_email(uid)
        if not addr:
            print(f"[notify] resolve: {uid} has no registered email")
        return addr
    except Exception as e:
        print(f"[notify] resolve: email lookup failed for {uid}: {e}")
        return None


def _send_completion_email(to, subject, html):
    """Send one notification over SMTP, authenticating with a password kept in a
    locked file (never in airflow.cfg or the metadata DB).

    Server and sender come from SLEDGE_SMTP_* env vars; the password is read from
    the file named by SLEDGE_SMTP_PASSWORD_FILE. If no sender is configured this
    is a no-op, so a run is never held up by mail setup. We send this ourselves
    rather than through airflow's send_email because that path only authenticates
    when an smtp_default Connection exists, and keeping the credential in the file
    is cleaner than putting it in the database.
    """
    import smtplib
    import ssl
    from email.message import EmailMessage

    user = os.environ.get("SLEDGE_SMTP_USER")
    pw_file = os.environ.get("SLEDGE_SMTP_PASSWORD_FILE")
    if not user or not pw_file:
        print("[notify] no SMTP sender configured (SLEDGE_SMTP_* unset); not sending")
        return
    host = os.environ.get("SLEDGE_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SLEDGE_SMTP_PORT", "587"))
    sender = os.environ.get("SLEDGE_SMTP_FROM") or user
    with open(pw_file) as f:
        password = f.read().strip()

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Your physical-design flow has finished. See the HTML version of this message for details.")
    msg.add_alternative(html, subtype="html")

    server = smtplib.SMTP(host, port, timeout=30)
    try:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(user, password)
        server.send_message(msg)
    finally:
        server.quit()


def notify_flow_complete(context):
    """DAG-level callback that emails the triggering user when their flow ends.

    Wired as both on_success_callback and on_failure_callback, so it fires once
    per run on either outcome. This is a DAG-level callback rather than logic in
    the exit_ task because exit_ uses a NONE_FAILED trigger rule and is skipped
    when an upstream fails, so it would miss failed runs. If no recipient is
    resolved (no triggering user, this DAG toggled off, or no registered
    address), this does nothing.
    """
    try:
        dag_run = context.get("dag_run") if isinstance(context, dict) else getattr(context, "dag_run", None)
        dag_id = getattr(dag_run, "dag_id", None) or "unknown"
        run_id = getattr(dag_run, "run_id", None) or "unknown"
        state = getattr(dag_run, "state", None) or "finished"
        conf = getattr(dag_run, "conf", None)
        if isinstance(conf, dict) and conf.get("notify") is False:
            return
        to = _resolve_notify_email(context)
        if not to:
            print(f"[notify] {dag_id} {run_id} ({state}): no recipient resolved, skipping")
            return
        subject = f"[Sledgehammer] {dag_id} {state}"
        html = (
            "Your physical-design flow has finished.<br><br>"
            f"DAG: {dag_id}<br>"
            f"Run: {run_id}<br>"
            f"Status: {state}"
        )
        _send_completion_email(to, subject, html)
        print(f"[notify] emailed {to} about {dag_id} {run_id} ({state})")
    except Exception as e:
        print(f"[notify] notification failed, ignoring: {e}")
