"""
Airflow plugin: a self-service "My Notification Email" page.

A logged-in user opens it from the nav and sets the address they want a note at
when one of their physical-design flows finishes, or clears it to opt out. The
address is keyed to the logged-in identity, so a user can only set their own.

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
from urllib.parse import parse_qs

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


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Notification email</title><style>
 body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1f2328; }
 h2 { margin: 0 0 .5rem; }
 input[type=email] { width: 22rem; padding: .45rem; font-size: 1rem; }
 button { padding: .5rem 1.1rem; font-size: 1rem; margin-left: .4rem; cursor: pointer; }
 .muted { color: #656d76; font-size: .9rem; }
 .ok { color: #1a7f37; }
 .err { color: #cf222e; }
 #form { display: none; }
</style></head><body>
<h2>Email me when my flows finish</h2>
<p class="muted">Signed in as <b id="who">&hellip;</b>. This is opt-in: set an
address to get a note when a physical-design run you triggered completes, or
clear it to stop.</p>
<p id="note"></p>
<form id="form">
  <input type="email" id="email" placeholder="you@berkeley.edu">
  <button type="submit">Save</button>
</form>
<p class="muted">Leave the box empty and Save to opt out.</p>
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
      email = document.getElementById('email');
  function setNote(msg, cls) { note.textContent = msg; note.className = cls || ''; }
  var T = token();
  if (!T) {
    who.textContent = '(unknown)';
    setNote('Please sign in to the Airflow UI first, then reload this page.', 'err');
    return;
  }
  var auth = { 'Authorization': 'Bearer ' + T };
  fetch('whoami', { headers: auth }).then(function (r) {
    if (r.status === 401 || r.status === 403) {
      throw new Error('Your Airflow session expired -- reload the Airflow UI and try again.');
    }
    if (!r.ok) throw new Error('Could not load your profile (HTTP ' + r.status + ').');
    return r.json();
  }).then(function (d) {
    who.textContent = d.uid || '(unknown)';
    email.value = d.current || d.email || '';
    form.style.display = 'block';
  }).catch(function (e) { who.textContent = '(unknown)'; setNote(e.message, 'err'); });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var body = 'email=' + encodeURIComponent(email.value.trim());
    fetch('save', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded' }, auth),
      body: body
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok && res.j.ok) {
        setNote(res.j.current
          ? 'Saved. You will be emailed at ' + res.j.current + ' when your flows finish.'
          : 'Opted out. You will not get completion emails.', 'ok');
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
    return {"uid": uid, "email": ldap_email, "current": current}


@app.post("/save")
async def save(request: Request, user: GetUserDep):
    from hammer.vlsi import pd_store
    uid, _ = _uid_and_default(user)  # identity from the validated token, never the body
    body = (await request.body()).decode("utf-8", "ignore")
    email = (parse_qs(body).get("email", [""])[0] or "").strip()
    try:
        if email:
            pd_store.set_notify_email(uid, email)
        else:
            pd_store.delete_notify_email(uid)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True, "current": email})


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
