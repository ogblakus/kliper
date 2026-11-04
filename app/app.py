import finder, hashlib, hmac, io, json, os, re, subprocess, threading, time, uuid, zipfile, queue
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse

import sys
APP = Path(__file__).resolve().parent; ROOT = Path(os.environ.get("CLIPPER_HOME", APP.parent))
OUT = Path(os.environ.get("CLIPPER_OUT", ROOT / "Klipy")); WORK = Path(os.environ.get("CLIPPER_WORK", ROOT / "work"))
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)
JOBS_F = DATA / "jobs.json"; LOGS = DATA / "logs"; LOGS.mkdir(exist_ok=True); SET_F = DATA / "settings.json"
# local mode: no password unless data/PASSWORD.txt exists (app listens on 127.0.0.1 only)
PW = (DATA / "PASSWORD.txt").read_text(encoding="utf-8").strip() if (DATA / "PASSWORD.txt").exists() else ""
DEFAULTS = {"model": "auto", "preset": "zbalansowany", "cookies_browser": "", "device": "auto"}
def settings():
    try: st = {**DEFAULTS, **json.loads(SET_F.read_text(encoding="utf-8"))}
    except Exception: st = dict(DEFAULTS)
    if not st.get("preset_set"):  # not chosen by user yet: CPU-only -> Szybki, NVIDIA -> Zbalansowany (detection is cached, done lazily)
        import hw; st["preset"] = "zbalansowany" if hw.detect()["gpu"] else "szybki"
    return st
def child_env():
    st = settings(); e = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", CLIPPER_HOME=str(ROOT), CLIPPER_OUT=str(OUT), CLIPPER_WORK=str(WORK))
    e["CLIP_COOKIES_FILE"] = str(DATA / "cookies.txt") if (DATA / "cookies.txt").exists() else ""
    e["CLIP_COOKIES_BROWSER"] = st["cookies_browser"]; e["CLIP_DEVICE"] = st["device"]; e["CLIP_WHISPER"] = st["model"]; e["CLIP_PRESET"] = st["preset"]; return e
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
# heavy work (whisper/ffmpeg) runs below normal priority so the PC stays responsive
LOWPRI = {"creationflags": 0x08000000 | 0x00004000} if os.name == "nt" else {"preexec_fn": lambda: os.nice(10)}
TOKEN = hmac.new(PW.encode(), b"clipper-session", hashlib.sha256).hexdigest()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = threading.Lock(); q = queue.Queue()
jobs = json.loads(JOBS_F.read_text(encoding="utf-8")) if JOBS_F.exists() else []
for j in jobs:
    if j["status"] in ("queued", "running"): j["status"] = "queued"; q.put(j["id"])

def save():
    with lock: JOBS_F.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
def get(jid):
    for j in jobs:
        if j["id"] == jid: return j
    raise HTTPException(404)

def worker():
    while True:
        jid = q.get(); j = next((x for x in jobs if x["id"] == jid), None)
        if not j or j["status"] != "queued": continue  # deleted or cancelled while waiting
        j.update(status="running", started=time.time(), stage="Pobieranie wideo…"); save()
        log = open(LOGS / f"{jid}.log", "w", encoding="utf-8")
        if j.get("kind") == "rerender":
            j["stage"] = "Renderuję ponownie…"; save()
            cmd = [sys.executable, "-u", str(APP / "main.py"), "--rerender", j["video_id"], "--clip", str(j["clip"])]
        else:
            cmd = [sys.executable, "-u", str(APP / "main.py"), j["url"], "--n", str(j["n"]), "--lang", j["lang"], "--model", settings()["model"], "--preset", settings()["preset"]] + (["--min", str(j["min"]), "--max", str(j["max"])] if j.get("min") else [])
        p = subprocess.Popen(cmd,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env(), cwd=str(APP), **LOWPRI)
        procs[jid] = p; last = ""
        for line in p.stdout:
            log.write(line); log.flush(); s = line.strip()
            if "transcribe model" in s: j["stage"] = "Transkrypcja" + (" wybranych fragmentów (szybka ścieżka: napisy YouTube)…" if "regions=" in s else " (to trwa najdłużej)…")
            elif "heatmap" in s: j["stage"] = "Szukam najlepszych momentów…"
            elif m := re.search(r"render (\d+)/(\d+)", s): j.setdefault("render_started", time.time()); j["stage"] = f"Montuję klip {m[1]} z {m[2]}…"; j["progress"] = int(m[1]) - 1; j["total"] = int(m[2])
            elif m := re.search(r"\[download\]\s+([\d.]+)%", s): j["stage"] = f"Pobieranie wideo… {float(m[1]):.0f}%"
            if s: last = s
        p.wait(); j["finished"] = time.time(); procs.pop(jid, None)
        if j.get("status") == "cancelled": save(); continue
        od = Path(last) if last and Path(last).resolve().parent == OUT.resolve() else None
        if p.returncode == 0 and od and (od / "summary.json").exists():
            sm = json.loads((od / "summary.json").read_text(encoding="utf-8"))
            j.update(status="done", stage="Gotowe", video_id=sm["video_id"], title=j.get("title") or sm.get("title"))
            refresh_clips(sm)
        else:
            err = last
            lt = open(LOGS / f"{jid}.log", encoding="utf-8", errors="replace").read()
            if "BOTCHECK" in lt or "Sign in to confirm" in lt: err = "YouTube prosi o zalogowanie (bot-check). Wgraj plik cookies.txt w Ustawieniach (instrukcja tam) i kliknij „Zrób klipy” jeszcze raz."
            elif "cookies" in lt.lower() and ("could not" in lt.lower() or "decrypt" in lt.lower()): err = "Nie da się odczytać ciasteczek z przeglądarki (Brave/Chrome/Edge je blokują). Wgraj plik cookies.txt w Ustawieniach."
            j.update(status="error", stage="Błąd: " + err[-300:])
        save()
def refresh_clips(sm):
    clips = [{"file": Path(c["file"]).name, "title": c["title"], "caption": c["caption"], "tags": " ".join(c.get("tags", [])),
              "hook": c["hook"], "score": c["score"], "duration": c["duration"], "ver": c.get("ver", 0)} for c in sm["clips"]]
    for x in jobs:
        if x.get("video_id") == sm["video_id"] and x.get("kind") != "rerender" and x["status"] == "done": x["clips"] = clips

procs = {}
threading.Thread(target=worker, daemon=True).start()

def authed(r: Request): return (not PW) or hmac.compare_digest(r.cookies.get("s", ""), TOKEN)
def need(r):
    if not authed(r): raise HTTPException(401)

CSS = """*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#eee}
.w{max-width:760px;margin:auto;padding:16px}h1{font-size:22px}input,select,button{font-size:17px;padding:12px;border-radius:10px;border:1px solid #333;background:#1b1e25;color:#eee;width:100%}
button{background:#ff3d6e;border:0;font-weight:700;cursor:pointer}button.s{background:#2b2f3a;width:auto;padding:8px 12px;font-size:14px}
.row{display:flex;gap:10px}.row>*{flex:1}.card{background:#171a21;border-radius:14px;padding:14px;margin:12px 0}
label{font-size:14px;color:#aaa;display:block;margin:10px 0 4px}video{width:100%;max-height:70vh;border-radius:10px;background:#000}
.st{font-size:14px;color:#aaa}.bar{height:8px;background:#2b2f3a;border-radius:4px;overflow:hidden}.bar>div{height:100%;background:#ff3d6e}
a{color:#ff7a9a}.btn{display:inline-block;background:#ff3d6e;color:#fff;padding:10px 14px;border-radius:10px;text-decoration:none;font-weight:700}
.tabs{display:flex;gap:8px;margin:8px 0}.tabs button{background:#2b2f3a}.tabs button.on{background:#ff3d6e}
.res{display:flex;gap:12px;align-items:flex-start}.res img{width:168px;border-radius:10px;flex:none;aspect-ratio:16/9;object-fit:cover}
.sc{background:#ff3d6e;border-radius:8px;padding:2px 8px;font-weight:700;font-size:14px}.ccb{background:#1f8f4e;border-radius:8px;padding:2px 8px;font-size:13px}
.why{margin:6px 0 8px 18px;padding:0;font-size:14px;color:#ccc}.chk{display:flex;align-items:center;gap:8px;margin:12px 0;color:#ccc}.chk input{width:22px;height:22px}
.info{position:relative;cursor:pointer;font-size:13px;color:#aaa;white-space:nowrap}.pop{display:none;position:absolute;z-index:9;left:0;top:22px;width:270px;background:#22262f;border:1px solid #3a3f4b;border-radius:10px;padding:8px 10px;color:#ddd;white-space:normal;box-shadow:0 6px 20px #0008}
@media(hover:hover){.info:hover .pop{display:block}}.info.open .pop{display:block}.pop .r{display:flex;justify-content:space-between;gap:8px;padding:2px 0;font-size:13px}.pop span{color:#999}.pop .g{color:#7ee2a0}.pop .m{color:#aaa;font-weight:400}
.warn{font-size:13px;color:#f5c26b;background:#2a2312;border-radius:10px;padding:10px}
@media(max-width:520px){.res{flex-direction:column}.res img{width:100%}}
.jh{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.ic{flex:none}.acts{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.steps{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0}.steps span{font-size:12px;padding:3px 8px;border-radius:10px;background:#2b2f3a;color:#888}.steps .on{background:#ff3d6e;color:#fff}.steps .ok{background:#1f5f3a;color:#cfc}
.err{background:#3a1518;border:1px solid #f66;border-radius:10px;padding:10px;margin:8px 0;word-break:break-word}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}.cap{font-size:14px;background:#0f1115;padding:8px;border-radius:8px;word-break:break-word}"""

LOGIN = f"""<!doctype html><html lang=pl><meta name=viewport content="width=device-width,initial-scale=1"><title>Clipper</title><meta charset=utf-8><style>{CSS}</style>
<div class=w><h1>🎬 Clipper – logowanie</h1><form method=post action=/login class=card><label>Hasło</label><input type=password name=pw autofocus>
<p></p><button>Zaloguj</button>__ERR__</form></div>"""

PAGE = f"""<!doctype html><html lang=pl><meta name=viewport content="width=device-width,initial-scale=1"><title>Clipper</title><meta charset=utf-8><style>{CSS}</style>
<div class=w><h1>🎬 Clipper – shorty z YouTube</h1>
<div class=tabs><button id=t1 class=on onclick="tab(1)">✂️ Klipy</button><button id=t2 onclick="tab(2)">🔎 Znajdź film</button></div>
<div id=p2 style="display:none"><form id=ff class=card><label>Temat (albo wybierz niszę)</label><input name=topic id=topic placeholder="np. Mentzen wywiad, AI agenci, Lewandowski…">
<label>Nisza</label><select name=preset>__PRESETS__</select>
<div class=row><div><label>Język</label><select name=lang><option value=pl>polski</option><option value=en>angielski</option></select></div>
<div><label>Wiek filmu</label><select name=age><option value=7>ostatnie 7 dni</option><option value=30>ostatnie 30 dni</option><option value=365>ostatni rok</option></select></div></div>
<label>Minimum wyświetleń: <b id=mvl>100 tys.</b></label><input type=range id=mvr min=0 max=7 step=0.25 value=5 oninput=mvU() style="padding:0">
<input type=hidden name=min_views id=mv value=100000><p class=st style="margin:2px 0">Suwak logarytmiczny 0 – 10 mln. Mniejsze filmy rzadko dają viralowe klipy.</p>
<label class=chk><input type=checkbox name=cc value=1> tylko Creative Commons (bezpieczne)</label>
<button id=fb>Szukaj 🔎</button><p class=st>Szukanie trwa zwykle 15–60 s (sprawdzam szczegóły ~15 najlepszych kandydatów). Wyniki są zapamiętywane na 6 h.</p>
<p class=warn>⚠️ Klipowanie cudzych filmów bez zgody autora może skończyć się roszczeniem lub ostrzeżeniem o naruszeniu praw autorskich (copyright strike). Najbezpieczniej: filmy z licencją Creative Commons (🟢 CC) albo za zgodą twórcy.</p></form>
<div id=fres></div></div>
<div id=p1><form id=f class=card><label>Link do filmu YouTube</label><input name=url id=url placeholder="https://www.youtube.com/watch?v=..." required inputmode=url>
<div class=row><div><label>Liczba klipów</label><input name=n type=number min=1 max=15 value=5></div>
<div><label>Język</label><select name=lang><option value=auto>automatycznie</option><option value=pl>polski</option><option value=en>angielski</option></select></div></div>
<p></p><button>Zrób klipy ✂️</button><p class=st>Film 10–15 min to zwykle 2–4 minuty czekania. Zadania idą po kolei, jedno naraz.</p></form>
<details class=card id=setc><summary><b>⚙️ Ustawienia</b> &nbsp;<button type=button class=s onclick="event.preventDefault();fetch('/api/open-folder',{{method:'POST'}})">📂 Otwórz folder z klipami</button></summary>
<label>Jakość / szybkość</label><select id=s_preset onchange=estUpd()><option value=szybki>⚡ Szybki</option><option value=zbalansowany>⚖️ Zbalansowany (zalecany)</option><option value=dokladny>🎯 Dokładny (wolny)</option></select>
<p class=st id=hwinfo></p>
<label>Model rozpoznawania mowy (Whisper)</label><select id=s_model><option value=auto>automatycznie (wg komputera i presetu)</option><option value=base>base – najszybszy</option><option value=small>small – szybki</option><option value=medium>medium – dokładny</option><option value=large-v3>large-v3 – najlepszy, bardzo wolny bez GPU</option></select>
<label>Karta graficzna (GPU)</label><select id=s_device><option value=auto>automatycznie (NVIDIA jeśli jest)</option><option value=cpu>tylko procesor</option></select>
<label>Ciasteczka YouTube z przeglądarki (tylko gdy YouTube prosi o zalogowanie)</label><select id=s_ck><option value="">wyłączone</option><option value=chrome>Chrome</option><option value=edge>Edge</option><option value=firefox>Firefox</option><option value=brave>Brave</option><option value=opera>Opera</option></select>
<p class=st>Brave/Chrome/Edge zwykle blokują ciasteczka – lepiej użyj pliku cookies.txt poniżej (albo Firefoxa).</p>
<label>🍪 Plik cookies.txt (zalecane, gdy YouTube prosi o zalogowanie)</label>
<ol class=st style="padding-left:18px;margin:4px 0"><li>W Brave/Chrome zainstaluj darmowe rozszerzenie <b>„Get cookies.txt LOCALLY”</b> (Chrome Web Store).</li><li>Wejdź na <b>youtube.com</b> (zalogowany).</li><li>Kliknij ikonę rozszerzenia → <b>Export</b> (format Netscape) – zapisze się plik <i>youtube.com_cookies.txt</i>.</li><li>Wybierz ten plik poniżej i kliknij „Wgraj”.</li></ol>
<input type=file id=ckf accept=".txt,text/plain"><p></p><div class=row><button type=button class=s onclick="upCk()">⬆ Wgraj cookies.txt</button><button type=button class=s onclick="delCk()">🗑 Usuń</button></div><p class=st id=ck_msg></p><button type=button onclick="saveSet()">Zapisz ustawienia</button><p class=st id=s_msg></p>
<button type=button class=s onclick="diag(this)">🩺 Diagnostyka</button><div id=dg></div></details>
<div id=jobs></div></div></div>
<script>
__JOBSJS__
async function loadSet(){{const st=await (await fetch('/api/settings')).json();ck_msg.textContent=st.cookies_file?'✅ Plik cookies.txt jest wgrany.':'Brak pliku cookies.txt.';s_model.value=st.model;s_device.value=st.device;s_ck.value=st.cookies_browser;s_preset.value=st.preset;ST=st;estUpd()}}
let ST=null;const PN={{szybki:'Szybki',zbalansowany:'Zbalansowany',dokladny:'Dokładny'}};
function estUpd(){{if(!ST)return;const h=ST.hw,tn={{gpu:'mocny (karta NVIDIA)',strong:'mocny procesor',mid:'średni',weak:'słabszy – zalecany Szybki'}}[ST.auto.tier];
 hwinfo.innerHTML=`💻 Wykryto: ${{h.cores}} wątków CPU, ${{h.ram_gb}} GB RAM, ${{h.gpu?'GPU '+esc(h.gpu):'brak karty NVIDIA'}} → <b>${{tn}}</b>. Model: <b>${{esc(ST.auto.model)}}</b>.<br>⏱ Szacowany czas dla filmu 30 min (5 klipów): `+
 Object.entries(ST.est30).map(([k,v])=>`${{k==s_preset.value?'<b>':''}}${{PN[k]}} ~${{v}} min${{k==s_preset.value?'</b>':''}}`).join(' · ')+`<br><span>Szybki i Zbalansowany używają napisów YouTube i transkrybują tylko wybrane fragmenty (gdy film ma napisy). Bez napisów ok. ${{ST.est30_full}} min.</span>`}}
async function upCk(){{const f=ckf.files[0];if(!f){{ck_msg.textContent='Najpierw wybierz plik.';return}}const fd=new FormData();fd.append('f',f);const r=await fetch('/api/cookies',{{method:'POST',body:fd}});const d=await r.json();ck_msg.textContent=r.ok?'✅ '+d.msg:'❌ '+(d.detail||'Błąd')}}
async function delCk(){{await fetch('/api/cookies',{{method:'DELETE'}});ck_msg.textContent='Usunięto plik cookies.'}}
async function saveSet(){{const r=await fetch('/api/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{preset:s_preset.value,model:s_model.value,device:s_device.value,cookies_browser:s_ck.value}})}});s_msg.textContent=r.ok?'Zapisano ✓':'Błąd';loadSet()}}
loadSet();
async function diag(b){{b.disabled=true;dg.innerHTML='<p class=st>Sprawdzam… (do 1–2 min)</p>';try{{const r=await fetch('/api/diag',{{method:'POST'}});const d=await r.json();
 dg.innerHTML=d.map(x=>`<p>${{x.ok?'✅':'❌'}} <b>${{esc(x.name)}}</b><br><span class=st style="word-break:break-word">${{esc(x.info)}}</span></p>`).join('')+'<p class=st>Zrób zrzut ekranu i wyślij. Log: folder Clipper\\data\\finder.log</p>'}}catch(e){{dg.innerHTML='❌ '+esc(e.message)}}b.disabled=false}}
function tab(n){{p1.style.display=n==1?'':'none';p2.style.display=n==2?'':'none';t1.className=n==1?'on':'';t2.className=n==2?'on':'';localStorage.tab=n}}
if(localStorage.tab==2)tab(2);
const fd=s=>{{s=s||0;const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+' h '+m+' min':m+' min'}};
const ag=d=>d<1?'dziś':d<2?'wczoraj':Math.round(d)+' dni temu';
let FR=[],FC=null;
function mvU(){{const x=+mvr.value,v=x<=0?0:Math.round(Math.pow(10,x)/(x>=6?1e5:x>=3?1e3:1))*(x>=6?1e5:x>=3?1e3:1);mv.value=v;mvl.textContent=v>=1e6?(v/1e6).toLocaleString('pl-PL')+' mln':v>=1e3?Math.round(v/1e3)+' tys.':v;localStorage.mvr=mvr.value}}
if(localStorage.mvr)mvr.value=localStorage.mvr;mvU();
function pop(x){{return '<span class=pop>'+x.stats.map(s=>`<span class=r><span>${{esc(s.k)}}</span><b class="${{s.ok===true?'g':s.ok===false?'m':''}}">${{esc(s.v)}}</b></span>`).join('')+'</span>'}}
document.addEventListener('click',e=>{{const t=e.target.closest('.info');document.querySelectorAll('.info.open').forEach(x=>x!==t&&x.classList.remove('open'));if(t)t.classList.toggle('open')}});
document.getElementById('ff').onsubmit=async e=>{{e.preventDefault();const b=fb;b.disabled=true;const t0=Date.now();
 const tk=setInterval(()=>b.textContent='Szukam… '+Math.round((Date.now()-t0)/1000)+' s',500);fres.innerHTML='';
 try{{const r=await fetch('/api/find',{{method:'POST',body:new FormData(e.target)}});if(r.status==401)return location.reload();
  const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);FR=d.results;FC=d.clip;
  fres.innerHTML=`<p class=st>${{d.results.length}} wyników · przejrzano ${{d.found}} filmów, szczegóły ${{d.deep}} · ${{d.cached?'z pamięci podręcznej':d.secs+' s'}}</p>`+
  ((d.errors||[]).length?'<div class=card style="border:1px solid #f66"><b>⚠️ Błędy wyszukiwania</b>'+d.errors.map(e=>'<p class=st style="word-break:break-word">'+esc(e)+'</p>').join('')+'<p class=st>Szczegóły: Clipper\\data\\finder.log · Ustawienia → Diagnostyka</p></div>':'')+(d.rights?'<p class=warn>'+esc(d.rights)+'</p>':'')+
  (d.results.length?'':'<div class=card>Brak wyników – zmniejsz minimum wyświetleń, wybierz 30 dni albo wyłącz filtr CC.</div>')+
  d.results.map((x,i)=>`<div class=card><div class=res><a href="${{x.url}}" target=_blank><img src="${{x.thumb}}" loading=lazy alt=""></a><div>
   <b>${{esc(x.title)}}</b><div class=st>${{esc(x.channel)}} · ${{x.views_h}} wyśw. · ${{ag(x.days)}} · ${{fd(x.duration)}}</div>
   <p><span class=sc>score ${{x.score}}</span> <span class=info tabindex=0>ℹ️ statystyki${{pop(x)}}</span> ${{x.cc?'<span class=ccb>🟢 CC – bezpieczne</span>':''}}</p></div></div>
   <b style="font-size:14px">Dlaczego:</b><ul class=why>${{x.why.map(w=>'<li>'+esc(w)+'</li>').join('')}}</ul>
   <button onclick="mk(this,${{i}})">Zrób klipy ✂️</button></div>`).join('');
 }}catch(err){{fres.innerHTML='<div class=card>❌ '+esc(err.message)+'</div>'}}finally{{clearInterval(tk);b.disabled=false;b.textContent='Szukaj 🔎'}}}};
async function mk(b,i){{const x=FR[i];const f=new FormData();f.append('url',x.url);f.append('n',5);f.append('lang',ff.lang.value);if(FC){{f.append('cmin',FC[0]);f.append('cmax',FC[1])}}
 b.disabled=true;const r=await fetch('/api/jobs',{{method:'POST',body:f}});if(!r.ok){{b.disabled=false;alert('Błąd');return}}
 b.textContent='✅ Dodano do kolejki – zobacz zakładkę Klipy';load()}}
</script>"""

@app.get("/api/settings")
def get_settings(r: Request):
    need(r); import hw; st = settings(); c = hw.pick(st["preset"], st["model"], st["device"]); h = c["hw"]
    est = {p: hw.estimate_min(30, hw.pick(p, st["model"], st["device"])["model"], c["tier"], h["cores"], hybrid=p != "dokladny") for p in hw.TABLE["mid"]}
    est_full = hw.estimate_min(30, c["model"], c["tier"], h["cores"], hybrid=False)
    return {**st, "cookies_file": (DATA / "cookies.txt").exists(), "hw": h, "auto": c, "est30": est, "est30_full": est_full}

from fastapi import UploadFile, File
@app.post("/api/cookies")
async def up_cookies(r: Request, f: UploadFile = File(...)):
    need(r); t = (await f.read(2_000_000)).decode("utf-8", "replace").replace("\r\n", "\n")
    lines = [l for l in t.split("\n") if l.strip() and not l.startswith("#") or l.startswith("#HttpOnly_")]
    yt = [l for l in lines if "youtube.com" in l and len(l.split("\t")) >= 7]
    if not yt: raise HTTPException(400, "To nie wygląda na plik cookies.txt z youtube.com (format Netscape). Wyeksportuj go rozszerzeniem „Get cookies.txt LOCALLY” będąc na youtube.com.")
    if not t.startswith("# Netscape") and not t.startswith("# HTTP Cookie"): t = "# Netscape HTTP Cookie File\n" + t
    (DATA / "cookies.txt").write_text(t, encoding="utf-8"); os.environ.update(child_env())
    return {"msg": f"Wgrano ({len(yt)} ciasteczek YouTube). Teraz spróbuj ponownie."}

@app.delete("/api/cookies")
def del_cookies(r: Request):
    need(r); (DATA / "cookies.txt").unlink(missing_ok=True); os.environ.update(child_env()); return {"ok": True}

@app.post("/api/settings")
async def set_settings(r: Request):
    need(r); b = await r.json(); st = settings()
    if b.get("model") in ("auto", "base", "small", "medium", "large-v3"): st["model"] = b["model"]
    if b.get("preset") in ("szybki", "zbalansowany", "dokladny"): st["preset"] = b["preset"]; st["preset_set"] = True
    if b.get("device") in ("auto", "cpu"): st["device"] = b["device"]
    if b.get("cookies_browser", "") in ("", "chrome", "edge", "firefox", "brave", "opera", "chromium", "vivaldi"): st["cookies_browser"] = b.get("cookies_browser", "")
    SET_F.write_text(json.dumps(st, indent=1), encoding="utf-8"); os.environ.update(child_env()); return st

@app.post("/api/open-folder")
def open_folder(r: Request, vid: str = ""):
    need(r); target = OUT / vid if vid and re.fullmatch(r"[\w-]+", vid) and (OUT / vid).is_dir() else OUT
    if os.name == "nt": os.startfile(str(target))
    elif sys.platform == "darwin": subprocess.Popen(["open", str(target)])
    else: subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}

@app.post("/api/diag")
def api_diag(r: Request):
    need(r); os.environ.update(child_env()); return finder.diagnose()

@app.post("/api/find")
def api_find(r: Request, topic: str = Form(""), preset: str = Form("podcasty"), lang: str = Form("pl"), age: int = Form(7), cc: str = Form(""), min_views: int = Form(100000)):
    need(r)
    os.environ.update(child_env())
    try: return {**finder.search(topic[:120], preset, lang, age, bool(cc), min_views=min_views),
                "rights": finder.RIGHTS_MSG if finder.PRESETS.get(preset, {}).get("rights") and not topic.strip() else "",
                "clip": finder.PRESETS.get(preset, {}).get("clip")}
    except Exception as e: raise HTTPException(500, f"Wyszukiwanie nie powiodło się: {e}")

JOBS_JS = (APP / "jobs.js").read_text(encoding="utf-8").replace("__LOCAL__", "true")

@app.get("/", response_class=HTMLResponse)
def index(r: Request):
    return PAGE.replace("__JOBSJS__", JOBS_JS).replace("__PRESETS__", "".join(f'<option value={k}>{v["label"]}</option>' for k, v in finder.PRESETS.items())) if authed(r) else LOGIN.replace("__ERR__", "")

@app.post("/login")
def login(pw: str = Form("")):
    if not hmac.compare_digest(pw.strip(), PW):
        time.sleep(1); return HTMLResponse(LOGIN.replace("__ERR__", "<p style='color:#f66'>Złe hasło</p>"), 401)
    resp = RedirectResponse("/", 303)
    resp.set_cookie("s", TOKEN, max_age=3600*24*180, httponly=True, secure=False, samesite="lax"); return resp

@app.get("/api/jobs")
def list_jobs(r: Request):
    need(r); return JSONResponse(list(reversed(jobs)))

@app.post("/api/jobs")
def add_job(r: Request, url: str = Form(...), n: int = Form(5), lang: str = Form("auto"), cmin: float = Form(0), cmax: float = Form(0)):
    need(r); url = url.strip()
    if not re.match(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url): raise HTTPException(400, "To nie wygląda na link YouTube.")
    if lang not in ("auto", "pl", "en"): lang = "auto"
    j = {"id": uuid.uuid4().hex[:10], "url": url, "n": max(1, min(int(n), 15)), "lang": lang, "status": "queued", "created": time.time()}
    if 10 <= cmin < cmax <= 120: j.update(min=cmin, max=cmax)
    jobs.append(j); save(); q.put(j["id"]); return j

@app.delete("/api/jobs/{jid}")
def del_job(r: Request, jid: str):
    need(r); j = get(jid)
    if j["status"] == "running" and jid in procs: j["status"] = "cancelled"; procs[jid].kill()
    jobs.remove(j); save(); return {"ok": True}

@app.post("/api/jobs/{jid}/cancel")
def cancel_job(r: Request, jid: str):
    need(r); j = get(jid)
    if j["status"] in ("queued", "running"):
        j.update(status="cancelled", stage="Przerwano", finished=time.time())
        if jid in procs:
            try: procs[jid].kill()
            except Exception: pass
        save()
    return j

@app.post("/api/jobs/{jid}/rerun")
def rerun_job(r: Request, jid: str):
    need(r); j = get(jid)
    nj = {k: j[k] for k in ("url", "n", "lang", "kind", "video_id", "clip", "title") if k in j}
    nj.update(id=uuid.uuid4().hex[:10], status="queued", created=time.time())
    jobs.append(nj); save(); q.put(nj["id"]); return nj

def clip_path(vid, f):
    if not re.fullmatch(r"[\w-]+", vid) or not re.fullmatch(r"clip_\d+\.mp4", f): raise HTTPException(404)
    p = OUT / vid / f
    if not p.exists(): raise HTTPException(404)
    return p

@app.get("/files/{vid}/{f}")
def files(r: Request, vid: str, f: str, dl: int = 0):
    need(r); p = clip_path(vid, f)
    return FileResponse(p, media_type="video/mp4", filename=f"{vid}_{f}" if dl else None)

@app.get("/zip/{jid}")
def zipall(r: Request, jid: str):
    need(r); j = get(jid)
    if j.get("status") != "done": raise HTTPException(404)
    zp = OUT / j["video_id"] / f"clips_{j['video_id']}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
        for c in j["clips"]: z.write(clip_path(j["video_id"], c["file"]), c["file"])
        z.write(OUT / j["video_id"] / "summary.md", "opisy.md")
    return FileResponse(zp, media_type="application/zip", filename=zp.name)


def summ_path(vid):
    if not re.fullmatch(r"[\w-]+", vid): raise HTTPException(404)
    p = OUT / vid / "summary.json"
    if not p.exists(): raise HTTPException(404)
    return p

@app.get("/api/clip/{vid}/{n}")
def clip_get(r: Request, vid: str, n: int):
    need(r); sm = json.loads(summ_path(vid).read_text(encoding="utf-8"))
    if not 1 <= n <= len(sm["clips"]): raise HTTPException(404)
    c = sm["clips"][n - 1]; ed = c.get("edits", {}).get("words", {})
    words = json.loads((WORK / vid / f"transcript_{sm['whisper_model']}.json").read_text(encoding="utf-8"))["words"]
    ws = []
    for w in words:
        if w["s"] >= c["start"] - 0.01 and w["e"] <= c["end"] + 0.01:
            k = f"{w['s']:.3f}"; e = ed.get(k, {})
            ws.append({"k": k, "t": round(w["s"] - c["start"], 2), "orig": w["w"], "w": e.get("w", w["w"]), "del": bool(e.get("del")), "p": w.get("p", 1),
                       "sug": w.get("sug"), "asr": w.get("asr")})
    if sm.get("language") == "pl" and not any(x.get("sug") or x.get("asr") for x in ws):  # older clips: suggestions only, no auto-fix
        try:
            import sys as _s; _s.path.insert(0, str(APP)); import spell
            tmp = [{"w": x["orig"], "p": x["p"]} for x in ws]; spell.check_words(tmp, auto=False)
            for x, t in zip(ws, tmp): x["sug"] = t.get("sug")
        except Exception as ex: print("spell:", ex)
    busy = any(x.get("kind") == "rerender" and x.get("video_id") == vid and x.get("clip") == n and x["status"] in ("queued", "running") for x in jobs)
    return {"start": c["start"], "end": c["end"], "title": c["title"], "caption": c["caption"], "file": Path(c["file"]).name,
            "ver": c.get("ver", 0), "words": ws, "busy": busy, "duration": c.get("duration")}

@app.post("/api/clip/{vid}/{n}")
async def clip_save(r: Request, vid: str, n: int):
    need(r); body = await r.json(); sp = summ_path(vid); sm = json.loads(sp.read_text(encoding="utf-8"))
    if not 1 <= n <= len(sm["clips"]): raise HTTPException(404)
    c = sm["clips"][n - 1]
    st, en = float(body["start"]), float(body["end"])
    if not (0 <= st < en and 5 <= en - st <= 120): raise HTTPException(400, "Zły zakres czasu (5–120 s).")
    c["start"], c["end"] = round(st, 3), round(en, 3)
    c["title"] = str(body.get("title", c["title"]))[:200]; c["caption"] = str(body.get("caption", c["caption"]))[:2000]
    ed = c.setdefault("edits", {}).setdefault("words", {})
    for k, v in (body.get("words") or {}).items():
        if re.fullmatch(r"\d+\.\d{3}", k): ed[k] = {"w": str(v.get("w", ""))[:60], "del": bool(v.get("del"))}
    sp.write_text(json.dumps(sm, ensure_ascii=False, indent=2), encoding="utf-8")
    refresh_clips(sm)
    rr = {"id": uuid.uuid4().hex[:10], "kind": "rerender", "video_id": vid, "clip": n, "url": "", "n": 1, "lang": "",
          "title": f"Ponowny render: klip {n} – {c['title']}", "status": "queued", "created": time.time()}
    jobs.append(rr); save(); q.put(rr["id"]); return rr

@app.get("/edit/{vid}/{n}", response_class=HTMLResponse)
def edit_page(r: Request, vid: str, n: int):
    if not authed(r): return RedirectResponse("/", 303)
    return EDIT.replace("__VID__", vid).replace("__N__", str(int(n)))

EDIT = """<!doctype html><html lang=pl><meta name=viewport content="width=device-width,initial-scale=1"><title>Edycja klipu</title><style>""" + CSS + """
.words{line-height:2.4}.wd{display:inline-block;margin:2px}.wd input{width:auto;padding:6px 8px;font-size:16px;border-radius:8px}
.wd.del input{text-decoration:line-through;opacity:.4}.wd.chg input{border-color:#ffcc00}.wd.low input{border-color:#ff7a3d}
.wd.sp input{border:2px dashed #b77dff}.wd.fx input{border:2px solid #4fc3f7}.chips{display:block;line-height:1.2}.chip{display:inline-block;background:#3a2b55;color:#e6d6ff;border:0;border-radius:12px;padding:3px 8px;margin:2px 2px 0 0;font-size:13px;width:auto;cursor:pointer}
.wd small{color:#777;font-size:11px;display:block;line-height:1;text-align:center}.x{background:none;border:0;color:#f66;width:auto;padding:2px 4px;font-size:15px}
.ln{border-bottom:1px solid #222;padding:4px 0}.nud{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.nud button{width:auto;padding:8px 10px;font-size:15px;background:#2b2f3a}
.nud b{min-width:70px;display:inline-block;text-align:center}.sticky{position:sticky;bottom:0;background:#0f1115;padding:10px 0}textarea{width:100%;font-size:16px;padding:10px;border-radius:10px;background:#1b1e25;color:#eee;border:1px solid #333}
</style><div class=w><p><a href="/">← Wróć do listy</a></p><h1>✏️ Edycja klipu __N__</h1>
<div class=card><video id=v controls playsinline preload=metadata></video><p class=st id=info></p></div>
<div class=card><b>Początek i koniec klipu</b><p class=st>Czas w oryginalnym filmie. Po zmianie zapisz, aby zobaczyć nowe słowa.</p>
<div class=nud>Start: <button onclick="nud('start',-2)">−2s</button><button onclick="nud('start',-0.5)">−0.5</button><b id=st></b><button onclick="nud('start',0.5)">+0.5</button><button onclick="nud('start',2)">+2s</button></div><p></p>
<div class=nud>Koniec: <button onclick="nud('end',-2)">−2s</button><button onclick="nud('end',-0.5)">−0.5</button><b id=en></b><button onclick="nud('end',0.5)">+0.5</button><button onclick="nud('end',2)">+2s</button></div></div>
<div class=card><b>Napisy</b><p class=st>Popraw słowo, dotknij ✕ żeby usunąć je z napisów (dźwięk zostaje). Pomarańczowa ramka = Whisper nie był pewny. <span style="color:#b77dff">Fioletowa przerywana</span> = brak w słowniku – dotknij podpowiedź. <span style="color:#4fc3f7">Niebieska</span> = poprawione automatycznie (podpowiedź przywraca oryginał). Liczba = sekunda w klipie.</p><div id=words class=words></div></div>
<div class=card><label>Tytuł</label><input id=title><label>Opis + hashtagi</label><textarea id=cap rows=4></textarea></div>
<div class=sticky><button id=go onclick="save()">🎬 Renderuj ponownie</button><p class=st id=msg></p></div></div>
<script>
const VID='__VID__',N=__N__;let D,ch={},rr=null;
const fmt=t=>{t=Math.max(0,t);const m=Math.floor(t/60);return m+':'+(t%60).toFixed(1).padStart(4,'0')};
function nud(k,d){D[k]=Math.max(0,Math.round((D[k]+d)*10)/10);if(D.end-D.start<5)D[k]-=d;show()}
function show(){st.textContent=fmt(D.start);en.textContent=fmt(D.end);info.textContent=`Długość źródła: ${(D.end-D.start).toFixed(1)} s · po wycięciu ciszy: ${D.duration} s`}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){const r=await fetch(`/api/clip/${VID}/${N}`);if(r.status==401)return location='/';D=await r.json();ch={};
 v.src=`/files/${VID}/${D.file}?v=${D.ver}`;title.value=D.title;cap.value=D.caption;show();
 let h='<div class=ln>',cnt=0;D.words.forEach((w,i)=>{const cls=(w.del?' del':'')+(w.w!=w.orig?' chg':'')+(w.p<0.5?' low':'');
  const sg=(w.asr?[w.asr]:[]).concat(w.sug||[]).filter(x=>x&&x.toLowerCase()!=w.w.toLowerCase().replace(/[^\\p{L}\\d-]+$/u,'')&&x!=w.w).slice(0,3);
  const cls2=cls+(w.asr&&w.w==w.orig?' fx':(w.sug&&w.sug.length&&w.w==w.orig?' sp':''));
  h+=`<span class="wd${cls2}" id=w${i}><input id=wi${i} value="${esc(w.w)}" size=${Math.max(2,w.w.length)} oninput="ed(${i},this.value)" title="${w.asr?'auto-poprawka z: '+esc(w.asr):''}"><button class=x onclick="del(${i})">✕</button><small>${w.t.toFixed(1)}</small>${sg.length?'<span class=chips>'+sg.map(x=>`<button class=chip onclick="pick(${i},this.textContent)">${esc(x)}</button>`).join('')+'</span>':''}</span>`;
  cnt++;if(/[.!?]$/.test(w.w)||cnt>=8){h+='</div><div class=ln>';cnt=0}});words.innerHTML=h+'</div>';
 busy(D.busy)}
function ed(i,val){const w=D.words[i];w.w=val;ch[w.k]={w:val,del:w.del};document.getElementById('w'+i).classList.toggle('chg',val!=w.orig)}
function pick(i,val){const inp=document.getElementById('wi'+i);const m=D.words[i].w.match(/^([^\\p{L}\\d]*).*?([^\\p{L}\\d]*)$/u);
 const v=/[^\\p{L}\\d]$/u.test(val)?val:val+(m?m[2]:'');inp.value=v;ed(i,v);const el=document.getElementById('w'+i);el.classList.remove('sp','fx')}
function del(i){const w=D.words[i];w.del=!w.del;ch[w.k]={w:w.w,del:w.del};document.getElementById('w'+i).classList.toggle('del',w.del)}
function busy(b){go.disabled=b;go.textContent=b?'⏳ Renderuję… (stara wersja zostaje do końca)':'🎬 Renderuj ponownie'}
async function save(){busy(true);msg.textContent='';
 const r=await fetch(`/api/clip/${VID}/${N}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start:D.start,end:D.end,title:title.value,caption:cap.value,words:ch})});
 if(!r.ok){busy(false);msg.textContent='Błąd: '+((await r.json()).detail||r.status);return}rr=(await r.json()).id;poll()}
async function poll(){const js=await (await fetch('/api/jobs')).json();const j=js.find(x=>x.id==rr);
 if(!j||j.status=='queued'||j.status=='running'){msg.textContent=j?(j.status=='queued'?'W kolejce…':j.stage):'';return setTimeout(poll,2500)}
 if(j.status=='done'){msg.textContent='✅ Gotowe – nowa wersja poniżej.';await load();window.scrollTo(0,0)}else{busy(false);msg.textContent='❌ '+j.stage}}
load();
</script>"""
