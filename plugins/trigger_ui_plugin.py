"""
Airflow plugin: a custom "Trigger a Flow" page.

Airflow's built-in trigger form is generated from a DAG's parameters and gives no
way for one toggle to flip the others. This page replicates that form for the
boolean parameters and adds the two things it can't do: a "Run all steps" switch
that actually ticks/unticks every step at once, and an "Email me when this
finishes" toggle whose status line shows the address it will mail (or warns if
none is registered). It triggers the run through Airflow's own REST API using the
signed-in user's token, so the run is recorded as triggered by that user -- the
same as the native button.

Auth note (Airflow 3.1.0): GetUserDep authenticates only from the Authorization:
Bearer header, never cookies, so the page itself is served without auth and its
JS reads the SPA token from same-origin localStorage and attaches it to every
call (the /trigger-ui data routes and Airflow's /api/v2 trigger endpoint).
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from airflow.plugins_manager import AirflowPlugin
from airflow.api_fastapi.core_api.security import GetUserDep


def _uid(user) -> str:
    return (getattr(user, "username", None) or user.get_name() or "").strip()


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Trigger a Flow</title><style>
 body{background:#0d1526;color:#e6e9ef;font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px}
 .wrap{max-width:760px}
 h2{font-weight:500;margin:0 0 14px}
 select{background:#131c2e;color:#e6e9ef;border:0.5px solid #2a3650;border-radius:8px;padding:9px 12px;font-size:15px;min-width:22rem}
 .sw{width:42px;height:24px;border-radius:999px;background:#39435c;position:relative;cursor:pointer;flex:none;border:none;padding:0;transition:background .15s}
 .sw[aria-checked=true]{background:#2f6fed}
 .sw .kn{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
 .sw[aria-checked=true] .kn{left:21px}
 .prow{display:flex;align-items:flex-start;gap:16px;padding:13px 2px;border-bottom:0.5px solid #1f2a40}
 .pl{font-weight:500;min-width:230px}
 .pd{color:#94a0b8;font-size:13px;margin-top:5px}
 .hl{background:#10203b;border-radius:8px;padding:13px 12px;border-bottom:none}
 button.go{background:#2f6fed;color:#fff;border:none;border-radius:8px;padding:9px 22px;font-size:15px;font-weight:500;cursor:pointer;margin-top:18px}
 .muted{color:#94a0b8;font-size:13px}
 #note{margin:12px 0;min-height:18px}
 .err{color:#fca5a5}.ok{color:#86efac}
</style></head><body>
<div class="wrap">
 <h2>Trigger a Flow</h2>
 <p class="muted" id="who">&hellip;</p>
 <div style="margin:14px 0"><select id="dag"><option value="">Select a DAG&hellip;</option></select></div>
 <p id="note"></p>
 <div id="body" style="display:none">
  <div style="font-size:16px;font-weight:500;margin:6px 0 6px">Run Parameters</div>
  <div class="prow hl">
   <div class="pl">Run all steps</div>
   <button class="sw" id="m-all" role="switch" aria-checked="false" aria-label="Run all steps"><span class="kn"></span></button>
   <div class="pd" style="margin-top:2px">Turn on every step below at once</div>
  </div>
  <div id="steps"></div>
  <div class="prow hl" style="margin-top:6px">
   <div class="pl">Email me when this finishes</div>
   <button class="sw" id="notify" role="switch" aria-checked="true" aria-label="Email me when this finishes"><span class="kn"></span></button>
   <div class="pd" id="nstat" style="margin-top:3px"></div>
  </div>
  <button class="go" id="go">Trigger</button>
 </div>
</div>
<script>
(function(){
 function on(e){return e.getAttribute('aria-checked')==='true';}
 function set(e,v){e.setAttribute('aria-checked',v?'true':'false');}
 function token(){try{var t=(window.parent&&window.parent.localStorage&&window.parent.localStorage.getItem('token'))||localStorage.getItem('token');if(t)return t;}catch(e){}var m=document.cookie.match(/(?:^|; )_token=([^;]+)/);return m?m[1]:null;}
 var who=document.getElementById('who'),note=document.getElementById('note'),dagSel=document.getElementById('dag'),
     body=document.getElementById('body'),stepsBox=document.getElementById('steps'),master=document.getElementById('m-all'),
     notify=document.getElementById('notify'),nstat=document.getElementById('nstat'),go=document.getElementById('go');
 function setNote(m,c){note.textContent=m;note.className=c||'';}
 var T=token();
 if(!T){who.textContent='Please sign in to the Airflow UI first, then reload this page.';return;}
 var auth={'Authorization':'Bearer '+T};
 var ADDR='';
 function checks(){return [].slice.call(stepsBox.querySelectorAll('.sw'));}
 function renderN(){
  if(!ADDR){nstat.innerHTML='<span style="color:#fbbf24">No address registered &mdash; set one on My Notification Email</span>';return;}
  if(on(notify)){nstat.innerHTML='<span style="color:#86efac">Will email '+ADDR+' when this finishes</span>';}
  else{nstat.textContent='Off — no email for this run';}
 }
 master.addEventListener('click',function(){var n=!on(master);set(master,n);checks().forEach(function(s){set(s,n);});});
 notify.addEventListener('click',function(){set(notify,!on(notify));renderN();});

 fetch('whoami',{headers:auth}).then(function(r){return r.json();}).then(function(d){
  who.textContent='Signed in as '+(d.uid||'(unknown)')+'.';ADDR=d.current||'';renderN();
 }).catch(function(){who.textContent='Signed in.';});

 fetch('dags',{headers:auth}).then(function(r){return r.json();}).then(function(d){
  (d.dags||[]).forEach(function(id){var o=document.createElement('option');o.value=id;o.textContent=id;dagSel.appendChild(o);});
 }).catch(function(e){setNote('Could not load DAGs: '+e.message,'err');});

 dagSel.addEventListener('change',function(){
  var id=dagSel.value;setNote('');
  if(!id){body.style.display='none';return;}
  fetch('params?dag_id='+encodeURIComponent(id),{headers:auth}).then(function(r){return r.json();}).then(function(d){
   stepsBox.innerHTML='';
   (d.params||[]).forEach(function(p){
    var row=document.createElement('div');row.className='prow';
    var lab=document.createElement('div');lab.className='pl';lab.textContent=p.title||p.name;
    var btn=document.createElement('button');btn.className='sw';btn.setAttribute('role','switch');
    btn.setAttribute('aria-checked',p.default?'true':'false');btn.setAttribute('aria-label',p.title||p.name);
    btn.dataset.name=p.name;btn.innerHTML='<span class="kn"></span>';
    btn.addEventListener('click',function(){set(btn,!on(btn));set(master,checks().every(on));});
    var de=document.createElement('div');de.className='pd';de.textContent=p.description||'';
    row.appendChild(lab);row.appendChild(btn);row.appendChild(de);stepsBox.appendChild(row);
   });
   set(master,checks().length>0&&checks().every(on));
   body.style.display='block';
  }).catch(function(e){setNote('Could not load parameters: '+e.message,'err');});
 });

 go.addEventListener('click',function(){
  var id=dagSel.value;if(!id){setNote('Pick a DAG first.','err');return;}
  var conf={};checks().forEach(function(c){conf[c.dataset.name]=on(c);});conf['notify']=on(notify);
  go.disabled=true;setNote('Triggering…');
  fetch('/api/v2/dags/'+encodeURIComponent(id)+'/dagRuns',{
   method:'POST',headers:Object.assign({'Content-Type':'application/json'},auth),
   body:JSON.stringify({logical_date:null,conf:conf})
  }).then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});}).then(function(res){
   go.disabled=false;
   if(res.ok){setNote('Triggered '+(res.j.dag_run_id||res.j.run_id||id)+(on(notify)&&ADDR?'  · you’ll be emailed at '+ADDR:''),'ok');}
   else{setNote('Could not trigger: '+(res.j.detail||JSON.stringify(res.j)),'err');}
  }).catch(function(e){go.disabled=false;setNote('Could not trigger: '+e.message,'err');});
 });
})();
</script>
</body></html>"""


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def show_page() -> str:
    return _PAGE


@app.get("/whoami")
def whoami(user: GetUserDep):
    from hammer.vlsi import pd_store
    uid = _uid(user)
    try:
        current = pd_store.get_notify_email(uid) or ""
    except Exception:
        current = ""
    return {"uid": uid, "current": current}


@app.get("/dags")
def list_dags(user: GetUserDep):
    """Every triggerable dag_id, for the picker."""
    import psycopg2
    from hammer.vlsi import pd_store
    try:
        settings = pd_store.airflow_metadata_conn_settings()
        conn = psycopg2.connect(**settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT dag_id FROM dag ORDER BY dag_id")
                dags = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        dags = []
    return {"dags": dags}


@app.get("/params")
def get_params(dag_id: str, user: GetUserDep):
    """Boolean parameters of a DAG, for rendering as step toggles.

    The dedicated notify toggle is handled separately, so 'notify' is excluded
    here. Non-boolean parameters aren't rendered by this page; a DAG that needs
    them should be triggered from Airflow's native form.
    """
    from airflow.models.serialized_dag import SerializedDagModel
    out = []
    try:
        dag = SerializedDagModel.get_dag(dag_id)
        if dag is not None:
            for name in dag.params:
                if name == "notify":
                    continue
                try:
                    p = dag.params.get_param(name)
                    schema = p.schema or {}
                    if schema.get("type") == "boolean":
                        out.append({
                            "name": name,
                            "title": schema.get("title") or name,
                            "description": schema.get("description") or "",
                            "default": bool(p.value),
                        })
                except Exception:
                    continue
    except Exception:
        pass
    return {"dag_id": dag_id, "params": out}


_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#2f6fed' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M7 4v16l13 -8z'/></svg>"
)
_ICON = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()


class TriggerUiPlugin(AirflowPlugin):
    name = "trigger_ui"
    # TODO: improve on the custom trigger page for future work. It's hidden from
    # the nav for now -- the native trigger form (with the "Run all steps" and
    # "Email me when this finishes" toggles) is the primary path, and a second
    # trigger page alongside the native button is redundant. The page is kept and
    # still served at /trigger-ui/; re-surface it by restoring the external_views
    # entry below.
    fastapi_apps = [
        {
            "name": "Trigger a Flow",
            "url_prefix": "/trigger-ui",
            "app": app,
        }
    ]
    # Hidden from the nav (see TODO above). To show it again, restore:
    #   {"name": "Trigger a Flow", "href": "/trigger-ui/", "url_route": "trigger-ui",
    #    "destination": "nav", "icon": _ICON}
    external_views = []
