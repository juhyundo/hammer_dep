"""Flow-completion email notifications.

Kept free of Airflow imports so the cluster policy can pull in the callback
without building any DAGs while settings load. Airflow 3 runs DAG callbacks in a
sandbox with no metadata-DB access and no triggering user in the context, so we
read the dag_run row over the SLEDGE_ channel (see pd_store).
"""

import os

from hammer.vlsi import pd_store


def _resolve_notify_email(context):
    """The address to notify for a finished run, or None to stay quiet.

    Opt-in per run: the run's "Email me when this finishes" toggle has to be on
    and the user has to have registered an address. The toggle only decides
    whether to send, never to whom.
    """
    dag_run = context.get("dag_run") if isinstance(context, dict) else getattr(context, "dag_run", None)
    dag_id = getattr(dag_run, "dag_id", None)
    run_id = getattr(dag_run, "run_id", None)
    conf = getattr(dag_run, "conf", None)
    if not (isinstance(conf, dict) and conf.get("notify") is True):
        return None
    # triggering_user_name is hidden on the runtime proxy, so read it from the row
    uid = getattr(dag_run, "triggering_user_name", None)
    if not uid:
        uid = pd_store.lookup_triggering_user(dag_id, run_id)
    if not uid or uid == "default":
        print(f"[notify] resolve: no triggering user for {dag_id} {run_id}")
        return None
    try:
        addr = pd_store.get_notify_email(uid)
        if not addr:
            print(f"[notify] resolve: {uid} asked for notify but has no registered email")
        return addr
    except Exception as e:
        print(f"[notify] resolve: email lookup failed for {uid}: {e}")
        return None


def _send_completion_email(to, subject, html):
    """Send one notification over SMTP, with the password read from a locked file.

    Host and sender come from SLEDGE_SMTP_* env vars; the password from the file
    at SLEDGE_SMTP_PASSWORD_FILE. A no-op if no sender is configured. We use
    smtplib directly because airflow's send_email only authenticates when an
    smtp_default Connection exists.
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
    """Email the triggering user when their flow ends.

    Wired as both on_success and on_failure so it fires on either outcome. It's
    a DAG-level callback rather than an exit_ task because exit_ uses a
    NONE_FAILED trigger rule and would be skipped on failures.
    """
    try:
        dag_run = context.get("dag_run") if isinstance(context, dict) else getattr(context, "dag_run", None)
        dag_id = getattr(dag_run, "dag_id", None) or "unknown"
        run_id = getattr(dag_run, "run_id", None) or "unknown"
        state = getattr(dag_run, "state", None) or "finished"
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
