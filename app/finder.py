"""Znajdź film: wyszukiwanie filmów YouTube z potencjałem na viralowe klipy (yt-dlp, bez API)."""
import base64, json, math, os, re, subprocess, sys, time, threading, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(os.environ.get("CLIPPER_HOME", Path(__file__).resolve().parent.parent))
CACHE_F = ROOT / "data" / "finder_cache.json"
PY = sys.executable
TTL = 6 * 3600
_lock = threading.Lock()

# clip: suggested clip length (s); rights: copyrighted film/cartoon material -> show notice in UI
PRESETS = {
    "podcasty": {"label": "Podcasty PL", "pl": ["podcast wywiad", "rozmowa podcast gość"], "en": ["podcast interview", "podcast episode guest"]},
    "podcast_en": {"label": "Podcast angielski", "lang": "en", "pl": ["podcast interview", "podcast episode guest"], "en": ["podcast interview full episode", "long form conversation podcast", "podcast clips viral guest story"]},
    "tworcy": {"label": "Twórcy (YouTuberzy, influencerzy)", "pl": ["youtuber wywiad", "twórca internetowy rozmowa", "influencer podcast szczerze"], "en": ["youtuber interview", "content creator podcast", "creator economy interview"]},
    "motywacja": {"label": "Motywacja", "pl": ["motywacja rozwój osobisty podcast", "mindset wywiad"], "en": ["motivation podcast", "mindset discipline interview"]},
    "biznes": {"label": "Biznes / pieniądze", "pl": ["biznes przedsiębiorca wywiad", "pieniądze inwestowanie podcast"], "en": ["business entrepreneur podcast", "money investing interview"]},
    "truecrime": {"label": "True crime", "pl": ["true crime po polsku", "kryminalna historia sprawa"], "en": ["true crime case", "true crime podcast"]},
    "detektyw": {"label": "Detektywistyczne / zagadki", "clip": [35, 75], "pl": ["zagadka kryminalna nierozwiązana sprawa", "śledztwo dokument po polsku", "detektyw historia sprawy"], "en": ["unsolved mystery documentary", "detective investigation case explained", "cold case solved"]},
    "urbex": {"label": "Urbex / opuszczone miejsca", "clip": [25, 55], "pl": ["urbex opuszczone miejsca", "urbex polska opuszczony", "eksploracja opuszczonego budynku"], "en": ["urbex abandoned", "exploring abandoned places", "abandoned mansion exploring"]},
    "podroze": {"label": "Podróże", "clip": [25, 50], "pl": ["podróże vlog", "podróż dookoła świata vlog", "wyjazd relacja z podróży"], "en": ["travel vlog", "travel documentary country", "backpacking adventure vlog"]},
    "skroty_filmy": {"label": "Skróty filmów / seriali", "rights": True, "clip": [40, 75], "pl": ["streszczenie filmu", "film w skrócie", "serial streszczenie odcinka"], "en": ["movie recap", "movie explained story recap", "series recap season"]},
    "bajki": {"label": "Bajki dla dzieci", "rights": True, "clip": [20, 45], "pl": ["bajki dla dzieci po polsku", "bajka dla dzieci cały odcinek", "kreskówki dla dzieci po polsku"], "en": ["cartoons for kids full episode", "kids cartoon episode", "animated story for children"]},
    "edu_dzieci": {"label": "Edukacyjne dla dzieci (skróty)", "rights": True, "clip": [20, 45], "pl": ["film edukacyjny dla dzieci", "nauka dla dzieci po polsku", "ciekawostki dla dzieci"], "en": ["educational videos for kids", "science for kids", "learning video for children"]},
    "humor": {"label": "Humor", "pl": ["stand-up podcast śmieszne", "kabaret rozmowa komik"], "en": ["comedy podcast", "comedian interview funny"]},
    "sport": {"label": "Sport", "pl": ["sport wywiad podcast", "piłka nożna rozmowa"], "en": ["sports podcast interview", "athlete interview"]},
    "tech": {"label": "Tech / AI", "pl": ["sztuczna inteligencja AI rozmowa", "technologia podcast"], "en": ["AI podcast interview", "tech podcast artificial intelligence"]},
}
RIGHTS_MSG = "⚖️ Materiały z filmów, seriali i bajek są zwykle chronione prawem autorskim. Odpowiadasz za posiadanie praw/zgody na ich użycie."
LONGFORM = re.compile(r"podcast|wywiad|rozmow|interview|odc\.|ep\.|episode|#\d+|gość|guest|q&a|live", re.I)

def _sp(age, dur, cc, by_views=True):
    # protobuf filters: upload date (3=week,4=month), type video, duration (2=>20min,3=4-20min), creative commons
    f = bytes([8, 3 if age == 7 else (4 if age == 30 else 5), 0x10, 1, 0x18, dur]) + (bytes([0x30, 1]) if cc else b"")
    # sort=view count (08 03): relevance order surfaces mostly tiny videos
    return base64.b64encode((bytes([0x08, 0x03]) if by_views else b"") + bytes([0x12, len(f)]) + f).decode()

def _env(): return dict(os.environ)
def _ck():
    c = os.environ.get("CLIP_COOKIES_FILE", "").strip()
    if c and Path(c).exists(): return ["--cookies", c]
    b = os.environ.get("CLIP_COOKIES_BROWSER", "").strip()
    return ["--cookies-from-browser", b] if b else []
BOT_MSG = ("YouTube prosi o zalogowanie (bot-check) przy pobieraniu szczegółów filmu. Wyniki pokazano na podstawie samego wyszukiwania. "
           "Aby to naprawić: Ustawienia → „Plik cookies.txt” (instrukcja obok) albo spróbuj później.")
COOKIE_MSG = ("Nie udało się odczytać ciasteczek z przeglądarki (Chrome/Brave/Edge blokują je, gdy są otwarte). "
              "Użyj pliku cookies.txt (Ustawienia) albo wyłącz ciasteczka z przeglądarki.")
def explain(err):
    e = err or ""
    if "cookie" in e.lower() and ("could not" in e.lower() or "decrypt" in e.lower() or "database" in e.lower() or "copy" in e.lower()): return COOKIE_MSG
    if "Sign in to confirm" in e or "not a bot" in e: return BOT_MSG
    return e[-300:]
BOT = {"hit": False}
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW

LOG_F = ROOT / "data" / "finder.log"
HL = {"v": "pl"}  # metadata language (YouTube auto-translates titles to the client language otherwise)
ERRORS = []  # errors of the current search, shown in UI

def flog(*a):
    msg = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        LOG_F.parent.mkdir(exist_ok=True)
        with open(LOG_F, "a", encoding="utf-8") as f: f.write(msg + "\n")
    except Exception: pass

def ytdlp(args, timeout):
    """Run yt-dlp from this Python (venv) with minimal, default settings. Returns (rc, stdout, stderr_tail)."""
    cmd = [PY, "-m", "yt_dlp", "--no-warnings", "--ignore-config", *_ck(), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=_env(), timeout=timeout, **NOWIN)
        tail = (r.stderr or "").strip()[-600:]
        if r.returncode: flog("FAIL rc", r.returncode, " ".join(args[-3:]), "|", tail)
        return r.returncode, r.stdout, tail
    except subprocess.TimeoutExpired:
        flog("TIMEOUT", " ".join(args[-3:])); return -1, "", f"przekroczony czas ({timeout} s)"
    except Exception as e:
        flog("EXC", repr(e)); return -2, "", repr(e)

def _entries(out):
    try: return json.loads(out).get("entries") or []
    except Exception as e: flog("bad json", repr(e), out[:200]); return []

def _flat(q, age, dur, cc, n):
    # 1) YouTube search page with filters (date, duration, CC) via sp=
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": q, "sp": _sp(age, dur, cc, HL["v"] == "en")})
    rc, out, err = ytdlp(["--extractor-args", "youtube:lang=en", "--flat-playlist", "-J", "--playlist-end", str(n), url], 90)
    E = _entries(out) if rc == 0 else []
    if not E:  # 2) plain ytsearch (no filters; age is checked later on deep metadata)
        rc, out, err2 = ytdlp(["--extractor-args", "youtube:lang=en", "--flat-playlist", "-J", f"ytsearch{n}:{q}"], 90)
        E = _entries(out) if rc == 0 else []
        if not E and (err or err2): ERRORS.append(f"Wyszukiwanie „{q}”: {explain(err2 or err)}")
    for i, e in enumerate(E): e["_rank"] = i
    flog(f"flat q={q!r} dur={dur} -> {len(E)}")
    return E

def _deep(vid):
    last = ""
    if BOT["hit"]: return None  # whole IP is bot-checked: don't waste time, use search data
    for cl in ["default", "tv", "web_safari", "android_vr"]:
        ea = ["--extractor-args", f"youtube:lang={HL['v']}" + ("" if cl == "default" else f";player_client={cl}")]
        rc, out, err = ytdlp([*ea, "-J", "--no-playlist", "--skip-download", "--ignore-no-formats-error",
                              f"https://www.youtube.com/watch?v={vid}"], 60)
        if rc == 0:
            try:
                d = json.loads(out)
                return {k: d.get(k) for k in ["id", "title", "channel", "duration", "view_count", "like_count", "comment_count",
                                              "channel_follower_count", "upload_date", "timestamp", "license", "heatmap", "thumbnail", "language"]}
            except Exception as e: err = repr(e)
        last = err
        if "cookie" in (err or "").lower() and "could not" in (err or "").lower(): break
    if "Sign in to confirm" in last or "not a bot" in last: BOT["hit"] = True
    ERRORS.append(explain(last) if (BOT["hit"] or "cookies" in last.lower()) else f"Szczegóły {vid}: {explain(last)}")
    return None

def _pre(e):
    v = e.get("view_count"); d = e.get("duration") or 0
    s = math.log10(v + 10) if v is not None else 6 - 0.1 * e.get("_rank", 30)
    if 600 <= d <= 7200: s += 1.5
    elif d > 7200: s += 0.3
    if LONGFORM.search(e.get("title") or ""): s += 0.8
    if HL["v"] == "pl" and not PLW.search(e.get("title") or ""): s -= 3  # prefer Polish titles for PL search
    return s

def _fmt(n):
    n = n or 0
    return f"{n/1e6:.1f} mln".replace(".0 ", " ") if n >= 1e6 else (f"{n/1e3:.0f} tys." if n >= 1e3 else str(n))

def _lin(x, lo, hi): return max(0.0, min((x - lo) / (hi - lo), 1.0))
def _t(sec): sec = int(sec); return f"{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}" if sec >= 3600 else f"{sec//60}:{sec%60:02d}"

def _score(d, age):
    """0–100. Big, fast-growing, engaging videos first; tiny videos are penalised (not surfaced)."""
    now = time.time(); ts = d.get("timestamp") or (time.mktime(time.strptime(d["upload_date"], "%Y%m%d")) if d.get("upload_date") else now - 86400 * age)
    days = max((now - ts) / 86400, 0.3); v = d.get("view_count") or 0; dur = d.get("duration") or 0
    subs = d.get("channel_follower_count") or 0; likes = d.get("like_count") or 0; com = d.get("comment_count") or 0
    if subs and subs < 2000 and v > 100 * subs: subs = 0  # yt-dlp sometimes returns a bogus tiny follower count
    vpd = v / days; lr = likes / v if v else 0; cr = com / v if v else 0; out = v / subs if subs else 0
    s_reach = _lin(math.log10(v + 1), 3.5, 6.5) * 25          # 3k -> 0, 3 mln -> max
    s_vel = _lin(math.log10(vpd + 1), 2.5, 5.3) * 20          # 300/day -> 0, 200k/day -> max
    s_out = _lin(out, 0.05, 1.5) * 15 if subs else 5           # views vs channel size
    s_eng = _lin(lr, 0.008, 0.05) * 10 + (_lin(cr, 0.0005, 0.005) * 5 if v >= 1000 else 0)
    if 1200 <= dur <= 7200: s_len = 12
    elif 600 <= dur < 1200: s_len = 9
    elif dur > 7200: s_len = 6
    else: s_len = 3
    lf = bool(LONGFORM.search(d.get("title") or ""))
    if lf: s_len = min(s_len + 3, 15)
    hm = d.get("heatmap") or []; s_hm = 0; peaks = []; ratio = 0
    if hm:
        vals = [h["value"] for h in hm]; mean = sum(vals) / len(vals); ratio = max(vals) / mean if mean else 0
        s_hm = _lin(ratio, 1.3, 3.5) * 10
        top = sorted(hm, key=lambda h: -h["value"])
        for h in top:  # 3 distinct peaks at least 60 s apart, skip the intro
            t = int(h["start_time"])
            if t > 20 and all(abs(t - p) > 60 for p in peaks): peaks.append(t)
            if len(peaks) == 3: break
        peaks.sort()
    score = s_reach + s_vel + s_out + s_eng + s_len + s_hm
    if v < 10000: score *= 0.6  # tiny videos rarely produce viral clips
    cc = (d.get("license") or "").lower().startswith("creative commons")
    nclips = max(1, min(10, int(dur / 420) + (len(peaks) if ratio > 1.8 else 0)))
    pot = "wysoki" if score >= 65 else ("średni" if score >= 45 else "niski")
    # short reasons (max 3) + compact stat rows for the hover/tap popup
    why = []
    if vpd >= 5000: why.append(f"szybko rośnie: {_fmt(vpd)} wyświetleń dziennie")
    if subs and out >= 0.8 and v >= 10000: why.append(f"{out:.1f}× więcej wyświetleń niż subskrypcji – temat chwycił")
    if ratio > 1.8 and peaks: why.append("widzowie wracają do: " + ", ".join(_t(p) for p in peaks))
    if lr >= 0.04: why.append(f"bardzo dobre polubienia ({100*lr:.1f}%)")
    if lf and len(why) < 3: why.append("długa rozmowa – dużo materiału na klipy")
    if cc: why.append("licencja CC – można klipować z podaniem autora")
    st = [("Wyświetlenia", _fmt(v), v >= 100000), ("Dziennie", _fmt(vpd), vpd >= 5000),
          ("Polubienia", f"{100*lr:.1f}%" if v else "–", lr >= 0.03), ("Komentarze", _fmt(com) + (f" ({100*cr:.2f}%)" if v and com else ""), cr >= 0.002),
          ("Długość", _t(dur) if dur else "–", 600 <= dur <= 7200), ("Kanał", (_fmt(subs) + " subskr.") if subs else "brak danych", None)]
    if subs: st.append(("Wyśw./subskr.", f"{out:.1f}×", out >= 0.8))
    st.append(("Najczęściej odtwarzane", ", ".join(_t(p) for p in peaks) if peaks else "brak danych", bool(peaks) and ratio > 1.8))
    st.append(("Potencjał", f"{pot} · ok. {nclips} klip{'y' if 2 <= nclips <= 4 else ('' if nclips == 1 else 'ów')}", score >= 55))
    return {"id": d["id"], "url": f"https://www.youtube.com/watch?v={d['id']}", "title": d.get("title"), "channel": d.get("channel"),
            "thumb": f"https://i.ytimg.com/vi/{d['id']}/hqdefault.jpg", "views": v, "views_h": _fmt(v), "days": round(days, 1),
            "duration": dur, "subs": subs, "likes": likes, "comments": com, "vpd": round(vpd), "cc": cc, "peaks": peaks,
            "score": round(score), "why": why[:3] or ["solidne wyniki w swojej niszy"], "lang": d.get("language"),
            "stats": [{"k": k, "v": x, "ok": ok} for k, x, ok in st], "clips_est": nclips}

PLW = re.compile(r"[ąćęłńóśźż]|\b(i|w|na|nie|jest|się|jak|czy|co|o|z|po|polsk\w*)\b", re.I)
def _lang_ok(r, lang):
    l = (r.get("lang") or "")[:2]
    if l: return l == lang
    if lang == "en": return not re.search(r"[ąćęłńśźż]", r["title"] or "", re.I)
    return bool(PLW.search(r["title"] or ""))

def _load():
    try: return json.loads(CACHE_F.read_text(encoding="utf-8"))
    except Exception: return {}

def search(topic="", preset="", lang="pl", age=7, cc=False, top=15, min_views=100000):
    lang = "en" if lang == "en" else "pl"; age = 7 if int(age) <= 7 else (30 if int(age) <= 30 else 365)
    mv = max(0, int(min_views or 0))
    queries = [topic.strip()] if topic.strip() else PRESETS.get(preset, PRESETS["podcasty"])[lang]
    key = json.dumps([queries, lang, age, bool(cc), mv], ensure_ascii=False)
    with _lock: c = _load()
    if key in c and time.time() - c[key]["t"] < TTL and c[key]["r"].get("results"): return {**c[key]["r"], "cached": True}
    t0 = time.time(); ERRORS.clear(); BOT["hit"] = False; HL["v"] = lang; flog("search", queries, lang, age, cc)
    jobs = [(q, 2, 30) for q in queries] + [(q, 3, 12) for q in queries]  # >20 min and 4-20 min
    with ThreadPoolExecutor(4) as ex: lists = list(ex.map(lambda a: _flat(a[0], age, a[1], cc, a[2]), jobs))
    seen = {}
    for L in lists:
        for e in L:
            if e.get("id") and e["id"] not in seen and (e.get("duration") or 0) >= 300 \
               and (e.get("view_count") is None or e["view_count"] >= mv * 0.7): seen[e["id"]] = e  # min views (flat counts are approximate)
    cands = sorted(seen.values(), key=_pre, reverse=True)[:top]
    with ThreadPoolExecutor(6 if (os.cpu_count() or 4) >= 8 else 3) as ex: dl = list(ex.map(lambda e: _deep(e["id"]), cands))
    for e, d in zip(cands, dl):  # keep original search title (detail fetch may return auto-translated titles)
        if d and e.get("title") and not d.get("title"): d["title"] = e["title"]  # deep uses hl=<search lang> -> original titles
    deep = [d for d in dl if d]
    # if details failed, still show the video using search data (marked)
    allv = deep + [{"id": e["id"], "title": e.get("title"), "channel": e.get("channel"), "duration": e.get("duration"),
                    "view_count": e.get("view_count"), "_flat": True} for e, d in zip(cands, dl) if not d]
    res = [_score(d, age) for d in allv]
    for r, d in zip(res, allv):
        if d.get("_flat"): r["why"].append("(brak szczegółów – ocena tylko z wyników wyszukiwania)")
    res = [r for r in res if r["views"] >= mv]  # server-side min-views filter
    strict = [r for r in res if r["days"] <= age + 1.5 and (r["cc"] or not cc) and _lang_ok(r, lang)]
    res = strict if strict else [r for r in res if r["cc"] or not cc]  # never hide everything because of filters
    res.sort(key=lambda r: -r["score"])
    out = {"results": res, "found": len(seen), "deep": len(deep), "secs": round(time.time() - t0, 1), "queries": queries, "t": time.time(),
           "errors": list(dict.fromkeys(ERRORS))[:5]}
    flog(f"done found={len(seen)} deep={len(deep)} results={len(res)} errors={len(ERRORS)} in {out['secs']}s")
    if not res: return {**out, "cached": False}  # never cache empty results
    with _lock:
        c = {k: v for k, v in _load().items() if time.time() - v["t"] < TTL}; c[key] = {"t": time.time(), "r": out}
        CACHE_F.parent.mkdir(exist_ok=True); CACHE_F.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    return {**out, "cached": False}

if __name__ == "__main__":
    r = search(*(sys.argv[1:2] or [""]), preset="podcasty", lang=(sys.argv[2] if len(sys.argv) > 2 else "pl"))
    print(r["secs"], r["found"], r["deep"])
    for x in r["results"][:8]: print(x["score"], x["views_h"], x["days"], x["title"][:70], "|", "; ".join(x["why"]))


def diagnose():
    """Small self-test for the Ustawienia → Diagnostyka button. Returns list of (name, ok, details)."""
    out = []
    rc, o, e = ytdlp(["--version"], 30); out.append(("yt-dlp", rc == 0, (o.strip() or e)[:300]))
    import shutil
    for exe in ("ffmpeg", "deno"):
        p = shutil.which(exe)
        if not p: out.append((exe, False, f"nie znaleziono {exe} w PATH")); continue
        try:
            r = subprocess.run([p, "-version" if exe == "ffmpeg" else "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, **NOWIN)
            out.append((exe, r.returncode == 0, (r.stdout or r.stderr).splitlines()[0][:200] if (r.stdout or r.stderr) else p))
        except Exception as ex: out.append((exe, False, repr(ex)))
    ERRORS.clear(); BOT["hit"] = False
    rc, o, e = ytdlp(["--extractor-args", "youtube:lang=en", "--flat-playlist", "-J", "ytsearch3:podcast wywiad"], 60); e = explain(e)
    E = _entries(o) if rc == 0 else []
    out.append(("Wyszukiwanie YouTube", bool(E), f"{len(E)} wyników: " + "; ".join((x.get('title') or '')[:40] for x in E) if E else e))
    t = time.time(); d = _deep(E[0]["id"]) if E else None
    out.append(("Szczegóły filmu / pobieranie", bool(d), f"{d['title'][:50]} · {d.get('view_count')} wyśw. · {round(time.time()-t,1)} s" if d else (ERRORS[-1] if ERRORS else "brak filmu do sprawdzenia")))
    BOT["hit"] = False
    ck = _ck(); out.append(("Ciasteczka", True, "plik cookies.txt" if ck[:1] == ["--cookies"] else (f"z przeglądarki: {ck[1]}" if ck else "wyłączone")))
    out.append(("Python", True, sys.version.split()[0] + " · " + sys.executable))
    flog("diagnose", out)
    return [{"name": n, "ok": ok, "info": i} for n, ok, i in out]
