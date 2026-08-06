from __future__ import annotations

import html
import json
from typing import Any

from fastapi.responses import HTMLResponse

from .config import APP_NAME, APP_VERSION

CSS = """
:root{
  color-scheme:dark;
  --bg:#0b1020;--bg-2:#0d1325;--card:#151c31;--card-2:#1b2440;--field:#0f1631;
  --line:#28325098;--line-solid:#2a3555;--text:#ecf1ff;--muted:#9aa7c7;--muted-dim:#6c7aa0;
  --ok:#42d392;--warn:#f7c948;--bad:#ff6b6b;--accent:#73a9ff;--accent-ink:#071022;
  --radius:12px;--radius-sm:8px;
  --shadow:0 10px 30px -12px rgba(0,0,0,.55);--shadow-sm:0 4px 14px -6px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body{
  margin:0;min-height:100vh;color:var(--text);
  background:
    radial-gradient(1100px 520px at 15% -10%, #14224a55, transparent 60%),
    radial-gradient(900px 480px at 100% 0%, #1a1f4a44, transparent 55%),
    var(--bg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,sans-serif;
  font-size:15px;line-height:1.55;
}
h1,h2,h3{font-weight:700;letter-spacing:-.01em;line-height:1.25;margin:0 0 14px}
h1{font-size:24px}
h2{font-size:17px}
h3{font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
p{margin:0 0 10px}

header{
  position:sticky;top:0;z-index:20;
  background:color-mix(in srgb, var(--bg-2) 88%, transparent);
  backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);
  padding:13px 22px;display:flex;gap:4px;align-items:center;flex-wrap:wrap;
}
header .brand{display:flex;align-items:center;gap:9px;margin-right:14px}
header .mark{
  width:26px;height:26px;border-radius:50%;flex:none;object-fit:cover;
  border:1.5px solid var(--accent);box-shadow:0 0 12px -2px var(--accent);
}
header strong{font-size:14px;letter-spacing:.02em}
header .muted{font-size:11.5px}
header nav{display:flex;gap:2px;flex-wrap:wrap;margin-left:10px}
header a{
  color:var(--muted);text-decoration:none;font-size:13.5px;
  padding:7px 11px;border-radius:7px;transition:color .15s,background .15s;
}
header a:hover{color:var(--text);background:#ffffff0c}

main{max-width:1180px;margin:0 auto;padding:32px 22px 64px}
main.centered{
  max-width:400px;min-height:calc(100vh - 60px);
  display:flex;flex-direction:column;justify-content:center;padding-top:0;padding-bottom:0;
}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.card{
  position:relative;
  background:linear-gradient(180deg, var(--card), color-mix(in srgb, var(--card) 92%, black));
  border:1px solid var(--line-solid);border-radius:var(--radius);
  padding:18px 20px;margin-bottom:16px;
  box-shadow:var(--shadow-sm), 0 0 0 1px transparent, 0 0 26px -14px var(--accent);
  transition:box-shadow .2s, border-color .2s;
}
.card:hover{border-color:#3b4a76;box-shadow:var(--shadow-sm), 0 0 30px -10px var(--accent)}
main.centered .card{margin-bottom:0;padding:26px 26px 22px}
.hero{
  display:flex;align-items:center;gap:18px;padding:20px 22px;margin-bottom:22px;
  background:radial-gradient(420px 160px at 0% 0%, #1a2a5c66, transparent 70%), linear-gradient(180deg, var(--card), color-mix(in srgb, var(--card) 90%, black));
  border:1px solid var(--line-solid);border-radius:var(--radius);box-shadow:0 0 40px -16px var(--accent);
}
.hero img{width:64px;height:64px;border-radius:50%;object-fit:cover;flex:none;border:1.5px solid var(--accent);box-shadow:0 0 26px -4px var(--accent)}
.hero h1{margin:0 0 4px}
.hero p{margin:0;color:var(--muted);font-size:13.5px}

.status{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:#28324d;color:var(--muted);font-size:11.5px;font-weight:600;letter-spacing:.02em}
.status::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.ready,.connected{color:var(--ok)}
.error,.permission_denied,.provider_unavailable{color:var(--bad)}
.not_configured,.authorization_required,.requires_provider_approval{color:var(--warn)}

label{display:block;font-size:12.5px;color:var(--muted);margin:0 0 5px;font-weight:500}
input,select,textarea{
  width:100%;padding:10px 12px;margin:0 0 14px;border-radius:var(--radius-sm);
  border:1px solid var(--line-solid);background:var(--field);color:var(--text);
  font-size:14px;font-family:inherit;transition:border-color .15s,box-shadow .15s;
}
input::placeholder,textarea::placeholder{color:var(--muted-dim)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px #73a9ff26}
textarea{min-height:90px;resize:vertical}

button,.button{
  cursor:pointer;appearance:none;
  background:var(--card-2);border:1px solid var(--line-solid);color:var(--text);
  text-decoration:none;display:inline-flex;align-items:center;gap:6px;
  padding:10px 15px;border-radius:var(--radius-sm);width:auto;font-size:13.5px;font-weight:600;
  transition:filter .15s,border-color .15s,transform .1s;
}
button:hover,.button:hover{border-color:#3b4a76;filter:brightness(1.12)}
button:active,.button:active{transform:translateY(1px)}
button:focus-visible,.button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
button:not(.secondary):not(.danger),form > button:only-of-type{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.danger{background:#3a1c22;border-color:#6b2f38;color:#ffb4bb}
.danger:hover{filter:brightness(1.25)}
.secondary{background:transparent;border-color:var(--line-solid);color:var(--muted)}
.secondary:hover{color:var(--text)}

table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;padding:9px 10px;color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--line-solid)}
td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#ffffff05}

code,pre{background:#0b1122;border:1px solid var(--line-solid);border-radius:7px;padding:3px 7px;overflow:auto;font-size:12.5px}
pre{padding:12px}
.muted{color:var(--muted)}
.flash{border-left:3px solid var(--accent)}
label.inline{display:flex;align-items:center;gap:8px;flex-direction:row}
label.inline input{width:auto;margin:0}
small{color:var(--muted);font-size:12px}

.auth-mark{width:96px;height:96px;border-radius:50%;object-fit:cover;border:1.5px solid var(--accent);box-shadow:0 0 44px -6px var(--accent);margin:0 auto 18px;display:block}
"""


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def layout(title: str, body: str, user: Any = None, csrf: str = "", centered: bool = False) -> HTMLResponse:
    nav = ""
    if user:
        admin = "<a href='/admin/users'>Χρήστες</a>" if user["role"] == "admin" else ""
        nav = (
            "<nav>"
            "<a href='/'>Αρχική</a><a href='/graph'>Γράφος</a><a href='/integrations'>Συνδέσεις</a>"
            "<a href='/family'>Οικογένεια</a><a href='/location'>Τοποθεσία</a><a href='/actions'>Ενέργειες</a>"
            "<a href='/media-library'>Media</a><a href='/memory'>Μνήμη</a><a href='/voice'>Φωνή</a>"
            f"<a href='/releases'>DistroKid</a>{admin}<a href='/account'>Λογαριασμός</a><a href='/logout'>Έξοδος</a>"
            "</nav>"
        )
    main_class = " class='centered'" if centered else ""
    return HTMLResponse(f"""<!doctype html><html lang='el'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)} · {APP_NAME}</title><style>{CSS}</style></head><body><header><div class='brand'><img class='mark' src='/static/avatar-mark.jpg' alt=''><strong>{APP_NAME}</strong><span class='muted'>{APP_VERSION}</span></div>{nav}</header><main{main_class}>{body}</main><script>window.ATHENA_CSRF={json.dumps(csrf)};async function api(url,options={{}}){{options.headers=Object.assign({{'X-CSRF-Token':window.ATHENA_CSRF}},options.headers||{{}});let r=await fetch(url,options);let t=await r.text();let d;try{{d=JSON.parse(t)}}catch{{d=t}}if(!r.ok)throw new Error(typeof d==='string'?d:(d.detail?.message||d.detail||JSON.stringify(d)));return d}}function pretty(x){{return JSON.stringify(x,null,2)}}async function confirmed(action,payload,execute){{let password=prompt('Επιβεβαίωση για '+action+'\\nΠληκτρολόγησε τον κωδικό σου:');if(!password)return;let c=await api('/api/confirmations',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action,payload,password}})}});return execute(c.token)}};</script></body></html>""")


def status_card(item: dict[str, Any]) -> str:
    status = esc(item.get("status", "unknown"))
    account = f"<div class='muted' style='margin-top:6px'>{esc(item.get('account',''))}</div>" if item.get("account") else ""
    error = f"<small>{esc(item.get('error',''))}</small>" if item.get("error") else ""
    return f"<div class='card'><h3 style='margin-bottom:8px'>{esc(item.get('provider',''))}</h3><span class='status {status}'>{status}</span>{account}{error}</div>"
