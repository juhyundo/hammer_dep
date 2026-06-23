"""
Airflow plugin: a self-service "My Notification Email" page.

A logged-in user opens it from the nav, sets the one address they want a note at
when a flow finishes, and ticks which DAGs should email them (each DAG is an
independent toggle; "Select all" flips them all at once). The address is keyed to
the logged-in identity, so a user can only set their own.

Auth note (Airflow 3.1.0): the api-server's GetUserDep authenticates only from
the Authorization: Bearer header -- it does not read cookies. A plain iframe
document load can't attach that header, so the page itself is served WITHOUT auth
and its JS reads the SPA's token (same-origin localStorage["token"], the same JWT
the Airflow UI sends as a bearer header) and calls the auth-guarded /whoami and
/save routes with it. The save stays validated server-side: the uid comes from
the verified token, never from the browser, so a user can only ever set their own.
"""

from __future__ import annotations

import base64
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from airflow.plugins_manager import AirflowPlugin
# GetUserDep guards the JSON routes (/whoami, /save), NOT the page load. On 3.1.0
# it authenticates only from the Authorization: Bearer header, so those routes are
# called by the page's JS with the SPA's token; the iframe document load is served
# unauthenticated on purpose.
from airflow.api_fastapi.core_api.security import GetUserDep


def _uid_and_default(user) -> tuple[str, str]:
    """The logged-in uid plus the LDAP address to offer as a pre-fill default."""
    uid = (getattr(user, "username", None) or user.get_name() or "").strip()
    ldap_email = (getattr(user, "email", None) or "").strip()
    return uid, ldap_email


def _list_all_dag_ids() -> list[str]:
    """Every DAG id from the metadata DB, so the page can offer a toggle per DAG.

    The api-server (where this plugin runs) is a normal Airflow process with the
    real metadata connection, so a direct read is fine here -- this is not the
    sandboxed callback path. Returns [] on any error so the page still loads with
    just the email field.
    """
    try:
        import psycopg2
        from hammer.vlsi import pd_store
        settings = pd_store.airflow_metadata_conn_settings()
        if not settings:
            return []
        conn = psycopg2.connect(**settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT dag_id FROM dag ORDER BY dag_id")
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Notification email</title><style>
 body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1f2328; }
 h2 { margin: 0 0 .5rem; }
 input[type=email] { width: 22rem; padding: .45rem; font-size: 1rem; }
 button { padding: .4rem 1rem; font-size: .95rem; cursor: pointer; }
 .muted { color: #656d76; font-size: .9rem; }
 .ok { color: #1a7f37; }
 .err { color: #cf222e; }
 #form { display: none; }
 .row { margin: .9rem 0; }
 .dagsbar { display: flex; align-items: center; gap: .5rem; margin: 1rem 0 .4rem; }
 .dagsbar b { margin-right: auto; }
 #dags { border: 1px solid #d0d7de; border-radius: 6px; max-height: 18rem;
         overflow-y: auto; padding: .4rem .6rem; }
 .dagrow { display: block; padding: .2rem 0; }
 .dagrow input { margin-right: .5rem; }
 #save { margin-top: 1rem; padding: .5rem 1.3rem; font-size: 1rem; }
</style></head><body>
<h2>Email me when my flows finish</h2>
<p class="muted">Signed in as <b id="who">&hellip;</b>. Set your address, then tick
the flows you want a note about when a run you triggered completes. Each flow is
its own toggle.</p>
<p id="note"></p>
<div id="form">
  <div class="row"><label>Email &nbsp;<input type="email" id="email" placeholder="you@berkeley.edu"></label></div>
  <div class="dagsbar">
    <b>Notify me for these flows</b>
    <button type="button" id="selall">Select all</button>
    <button type="button" id="clrall">Clear all</button>
  </div>
  <div id="dags"></div>
  <button type="button" id="save">Save</button>
  <p class="muted">Clear the email box (or untick everything) and Save to stop emails.</p>
</div>
<script>
(function () {
  function token() {
    try {
      var t = (window.parent && window.parent.localStorage && window.parent.localStorage.getItem('token'))
              || localStorage.getItem('token');
      if (t) return t;
    } catch (e) {}
    var m = document.cookie.match(/(?:^|; )_token=([^;]+)/);
    return m ? m[1] : null;
  }
  var who = document.getElementById('who'),
      note = document.getElementById('note'),
      form = document.getElementById('form'),
      email = document.getElementById('email'),
      dagsBox = document.getElementById('dags');
  function setNote(msg, cls) { note.textContent = msg; note.className = cls || ''; }
  function checks() { return Array.prototype.slice.call(dagsBox.querySelectorAll('.dagcb')); }
  function setAll(v) { checks().forEach(function (c) { c.checked = v; }); }

  var T = token();
  if (!T) {
    who.textContent = '(unknown)';
    setNote('Please sign in to the Airflow UI first, then reload this page.', 'err');
    return;
  }
  var auth = { 'Authorization': 'Bearer ' + T };

  function renderDags(dags) {
    dagsBox.innerHTML = '';
    if (!dags || !dags.length) { dagsBox.textContent = 'No DAGs found.'; return; }
    dags.forEach(function (d) {
      var row = document.createElement('label');
      row.className = 'dagrow';
      var cb = document.createElement('input');
      cb.type = 'checkbox'; cb.className = 'dagcb'; cb.value = d.id; cb.checked = !!d.enabled;
      row.appendChild(cb);
      row.appendChild(document.createTextNode(d.id));
      dagsBox.appendChild(row);
    });
  }

  fetch('whoami', { headers: auth }).then(function (r) {
    if (r.status === 401 || r.status === 403) {
      throw new Error('Your Airflow session expired -- reload the Airflow UI and try again.');
    }
    if (!r.ok) throw new Error('Could not load your profile (HTTP ' + r.status + ').');
    return r.json();
  }).then(function (d) {
    who.textContent = d.uid || '(unknown)';
    email.value = d.current || d.email || '';
    renderDags(d.dags);
    form.style.display = 'block';
  }).catch(function (e) { who.textContent = '(unknown)'; setNote(e.message, 'err'); });

  document.getElementById('selall').addEventListener('click', function () { setAll(true); });
  document.getElementById('clrall').addEventListener('click', function () { setAll(false); });

  document.getElementById('save').addEventListener('click', function () {
    var chosen = checks().filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
    fetch('save', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, auth),
      body: JSON.stringify({ email: email.value.trim(), dags: chosen })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok && res.j.ok) {
        if (!res.j.current) {
          setNote('Saved. No email set, so you will not get completion emails.', 'ok');
        } else if (!res.j.count) {
          setNote('Saved. No flows ticked, so you will not get completion emails.', 'ok');
        } else {
          setNote('Saved. ' + res.j.current + ' will be emailed for ' + res.j.count + ' flow(s).', 'ok');
        }
      } else {
        setNote('Could not save: ' + (res.j.error || 'unknown error'), 'err');
      }
    }).catch(function (e) { setNote('Could not save: ' + e.message, 'err'); });
  });
})();
</script>
</body></html>"""


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def show_form() -> str:
    # Served with NO auth: an iframe document GET can't carry the Bearer header
    # GetUserDep needs, so the page is static and its JS authenticates the
    # /whoami and /save calls instead.
    return _PAGE


@app.get("/whoami")
def whoami(user: GetUserDep):
    from hammer.vlsi import pd_store
    uid, ldap_email = _uid_and_default(user)
    try:
        current = pd_store.get_notify_email(uid) or ""
    except Exception:
        current = ""
    try:
        enabled = set(pd_store.list_enabled_dags(uid))
    except Exception:
        enabled = set()
    dags = [{"id": d, "enabled": d in enabled} for d in _list_all_dag_ids()]
    return {"uid": uid, "email": ldap_email, "current": current, "dags": dags}


@app.post("/save")
async def save(request: Request, user: GetUserDep):
    from hammer.vlsi import pd_store
    uid, _ = _uid_and_default(user)  # identity from the validated token, never the body
    try:
        payload = json.loads((await request.body()).decode("utf-8", "ignore") or "{}")
    except Exception:
        payload = {}
    email = (payload.get("email") or "").strip()
    dags = payload.get("dags") or []
    if not isinstance(dags, list):
        dags = []
    try:
        if email:
            pd_store.set_notify_email(uid, email)
        else:
            pd_store.delete_notify_email(uid)
        pd_store.set_enabled_dags(uid, [str(d) for d in dags])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True, "current": email, "count": len(dags)})


_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#673ab7' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    "<rect x='2' y='4' width='20' height='16' rx='2'/>"
    "<path d='m22 7-10 6L2 7'/></svg>"
)
_ICON = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()


class NotifyEmailPlugin(AirflowPlugin):
    name = "notify_email"
    fastapi_apps = [
        {
            "name": "Notification Email",
            "url_prefix": "/notify-email",
            "app": app,
        }
    ]
    external_views = [
        {
            "name": "My Notification Email",
            "href": "/notify-email/",
            "url_route": "notify-email",
            "destination": "nav",
            "icon": _ICON,
        }
    ]
