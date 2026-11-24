#!/usr/bin/env python3
"""clip: darmowy auto-clipper (yt-dlp + faster-whisper + heurystyka/LLM + OpenCV + ffmpeg)."""
import argparse, json, os, re, subprocess, sys, time, math, shutil, urllib.request
from pathlib import Path
import numpy as np

APP = Path(__file__).resolve().parent                     # .../Clipper/app
ROOT = Path(os.environ.get("CLIPPER_HOME", APP.parent))   # .../Clipper
OUT = Path(os.environ.get("CLIPPER_OUT", ROOT / "Klipy"))
WORK = Path(os.environ.get("CLIPPER_WORK", ROOT / "work"))
YUNET = APP / "models" / "yunet.onnx"
FONTS = APP / "fonts"

def log(*a): print("[clip]", *a, file=sys.stderr, flush=True)
def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)

# ---------------------------------------------------------------- download
CLIENTS = ["default", "tv", "web_safari", "android_vr", "ios", "mweb", "tv_embedded", "web_embedded"]
def ytdlp_extra():
    """yt-dlp cookie args from settings: cookies.txt file (preferred) or browser (default off)."""
    f = os.environ.get("CLIP_COOKIES_FILE", "").strip()
    if f and Path(f).exists(): return ["--cookies", f]
    b = os.environ.get("CLIP_COOKIES_BROWSER", "").strip()
    return ["--cookies-from-browser", b] if b else []

def _slug(t, n=50):
    import unicodedata
    t = (t or "").translate(str.maketrans("łŁøØđĐ", "lLoOdD")); t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")[:n].strip("_")

def _folder_name(info, wdir):
    """Readable folder name: <date>_<title>__<youtubeId>. Reuses an existing folder of the same video (also the old plain-ID one)."""
    yid = info["id"]
    for c in list(wdir.glob(f"*__{yid}")) + [wdir / yid]:
        if c.is_dir(): return c.name
    t = _slug(info.get("title"))
    return f"{time.strftime('%Y-%m-%d')}_{t}__{yid}" if t else yid

def download(url, wdir, lang="auto"):
    if Path(url).exists():  # local file
        vid = re.sub(r"\W+", "_", Path(url).stem)[:40]
        d = wdir / vid; d.mkdir(parents=True, exist_ok=True)
        dst = d / "video.mp4"
        if not dst.exists(): shutil.copy(url, dst)
        return vid, d, {"title": Path(url).stem, "id": vid}
    env = dict(os.environ)
    ytdlp = [sys.executable, "-m", "yt_dlp", *ytdlp_extra()]
    info = None
    errs = []
    for clients in CLIENTS:
        ea = [] if clients == "default" else ["--extractor-args", f"youtube:player_client={clients}"]
        try:
            j = subprocess.run(ytdlp + ea + ["-J", "--no-playlist", url], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180)
            if j.returncode: errs.append(j.stderr.strip()[-300:]); log("info fail", clients, j.stderr.strip()[-200:])
            if j.returncode == 0:
                info = json.loads(j.stdout); fmts = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")]
                if fmts: break
                info = None
        except Exception as e:
            log("info fail", clients, e)
    if not info:
        bot = any("Sign in" in e or "bot" in e for e in errs)
        raise SystemExit(("BOTCHECK: " if bot else "") + "yt-dlp nie mógł pobrać filmu. Ostatni błąd: " + (errs[-1] if errs else "?"))
    vid = _folder_name(info, wdir); d = wdir / vid; d.mkdir(parents=True, exist_ok=True)
    (d / "info.json").write_text(encoding="utf-8", data=json.dumps({k: info.get(k) for k in ["id","title","duration","heatmap","uploader","license","webpage_url","chapters","tags","description","language"]}, ensure_ascii=False))
    import threading
    th = threading.Thread(target=fetch_autosubs, args=(info, d, lang), daemon=True); th.start()  # in parallel with the video download
    dst = d / "video.mp4"
    if not dst.exists():
        fmt = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
        ea = [] if clients == "default" else ["--extractor-args", f"youtube:player_client={clients}"]
        log("download", url, "client", clients)
        run(ytdlp + ea + ["-f", fmt, "--merge-output-format", "mp4", "--no-playlist", "-o", str(d / "video.%(ext)s"), url], env=env)
        if not dst.exists():
            c = [p for p in d.glob("video.*") if p.suffix in (".mkv", ".webm")]
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(c[0]), "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(dst)])
    th.join(30)
    return vid, d, info

def autosub_key(ac, lang, info_lang=""):
    """Prefer {lang}-orig, then the video's {info_lang}-orig, else the first *-orig track."""
    if not ac: return None
    req, info = (lang or "").split("-")[0], (info_lang or "").split("-")[0]
    for code in (req, info):
        if code and code != "auto" and f"{code}-orig" in ac: return f"{code}-orig"
    keys = [k for k in ac if k.endswith("-orig")] + ([info] if info and info in ac else [])
    return keys[0] if keys else None

def fetch_autosubs(info, d, lang="auto"):
    """Fast path: YouTube auto-captions (json3, word timings) in the original language -> d/autosubs.json."""
    out = d / "autosubs.json"
    if out.exists(): return
    try:
        ac = info.get("automatic_captions") or {}
        key = autosub_key(ac, lang, info.get("language") or "")
        if not key: return
        url = next((f["url"] for f in ac[key] if f.get("ext") == "json3"), None)
        if not url: return
        import urllib.request
        ev = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25).read().decode("utf-8")).get("events", [])
        words = []
        for e in ev:
            t0 = e.get("tStartMs", 0) / 1000; segs = [x for x in e.get("segs") or [] if x.get("utf8", "").strip()]
            for i, x in enumerate(segs):
                s0 = t0 + x.get("tOffsetMs", 0) / 1000
                for k, w in enumerate(x["utf8"].split()): words.append({"w": w, "s": round(s0 + 0.01 * k, 3), "e": 0, "p": 0.5})
            if words: words[-1]["e"] = t0 + (e.get("dDurationMs") or 0) / 1000
        for i, w in enumerate(words):
            nx = words[i + 1]["s"] if i + 1 < len(words) else w["s"] + 0.5
            est = w["s"] + 0.12 + 0.065 * len(w["w"])  # YouTube gives only word starts: estimate the end so pauses stay visible
            w["e"] = round(max(w["s"] + 0.05, min(nx - 0.02, est, w["e"] or est)), 3)
        if len(words) > 50:
            out.write_text(json.dumps({"language": key.split("-")[0], "words": words}, ensure_ascii=False), encoding="utf-8"); log(f"autosubs {key} words={len(words)}")
    except Exception as e:
        log("autosubs unavailable:", e)

# ---------------------------------------------------------------- transcribe
def _cuda_dlls():
    """Windows: make pip-installed NVIDIA cuBLAS/cuDNN DLLs visible to ctranslate2."""
    if os.name != "nt": return
    import site
    for sp in site.getsitepackages():
        for bdir in Path(sp).glob("nvidia/*/bin"):
            try: os.add_dll_directory(str(bdir))
            except OSError: pass
            os.environ["PATH"] = str(bdir) + os.pathsep + os.environ.get("PATH", "")

_WM = {}
def load_whisper(model_name):
    if model_name in _WM: return _WM[model_name]
    from faster_whisper import WhisperModel
    dev = os.environ.get("CLIP_DEVICE", "auto"); m = None
    if dev in ("auto", "cuda"):
        try:
            _cuda_dlls(); import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                m = WhisperModel(model_name, device="cuda", compute_type="float16"); m._gpu = True
                log("whisper on GPU (CUDA float16)")
        except Exception as e:
            log("GPU unavailable, using CPU:", e); m = None
    if m is None:
        m = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4) - 1)); m._gpu = False  # leave 1 core for the system
    _WM[model_name] = m; return m

def _drop_loops(ws):
    """Whisper hallucination: the same word repeated 4+ times in a row ("ktop ktop ktop ...") -> drop the whole run."""
    out, i = [], 0
    while i < len(ws):
        j = i
        k = re.sub(r"[^\w]", "", ws[i]["w"].lower())
        while j + 1 < len(ws) and re.sub(r"[^\w]", "", ws[j + 1]["w"].lower()) == k: j += 1
        if j - i + 1 >= 4: i = j + 1; continue
        out += ws[i:j + 1]; i = j + 1
    return out

def _drop_repeat_segs(segs):
    """Drop a segment that repeats the previous segment verbatim (hallucination loop)."""
    out, prev = [], ""
    for s in segs:
        t = re.sub(r"\s+", " ", (getattr(s, "text", "") or "")).strip().lower()
        if t and t == prev: continue
        if t: prev = t
        out.append(s)
    return out

def _drop_phrase_loops(ws):
    """Drop a multi-word phrase said 3+ times in a row. Cached transcripts have no segment breaks."""
    ws = _drop_loops(ws)
    if len(ws) < 6: return ws
    toks = [re.sub(r"[^\w]", "", w["w"].lower()) for w in ws]
    drop = [False] * len(ws); i = 0
    while i < len(ws):
        found = False
        for n in range(2, 7):
            if i + 3 * n > len(ws): continue
            gram = toks[i:i + n]
            if not any(gram) or toks[i:i + 3 * n] != gram * 3: continue
            j = i + 3 * n
            while j + n <= len(ws) and toks[j:j + n] == gram: j += n
            for k in range(i, j): drop[k] = True
            i = j; found = True; break
        if not found: i += 1
    return [w for w, d in zip(ws, drop) if not d]

def _words(segs, offset):
    segs = _drop_repeat_segs(segs)
    return _drop_phrase_loops([{"w": w.word.strip(), "s": round(w.start + offset, 3), "e": round(w.end + offset, 3), "p": round(w.probability, 3)}
            for s in segs for w in (s.words or []) if w.word.strip()])

def _run_whisper(m, audio, lang, beam, prompt, offset=0.0):
    # condition_on_previous_text=False stops the loop feeding itself. compression_ratio_threshold
    # (2.4) and no_speech_threshold (0.6) are the library defaults and already fail compressed or
    # silent segments. hallucination_silence_threshold drops end-card silence that still emits text.
    kw = dict(language=None if lang == "auto" else lang, word_timestamps=True, vad_filter=True, beam_size=beam,
              initial_prompt=prompt or None, condition_on_previous_text=False, hallucination_silence_threshold=2.0)
    words = None
    # batched pipeline is ~2x faster but faster-whisper 1.2 often returns words for only a few segments -> verify, else fall back
    if os.environ.get("CLIP_BATCHED", "1" if m._gpu else "0") == "1" and len(audio) > 16000 * 40:
        try:
            from faster_whisper import BatchedInferencePipeline
            segs, inf = BatchedInferencePipeline(m).transcribe(audio, batch_size=16 if m._gpu else 8, **kw); segs = list(segs)
            words = _words(segs, offset); ntxt = sum(len(s.text.split()) for s in segs)
            if len(words) < 0.8 * ntxt: log(f"batched: words missing ({len(words)}/{ntxt}), falling back"); words = None
        except Exception as e: log("batched unavailable:", e); words = None
    if words is None: segs, inf = m.transcribe(audio, **kw); words = _words(segs, offset)
    return words, inf.language

def _ensure_wav(d):
    wav = d / "audio16k.wav"
    if not wav.exists() or wav.stat().st_size < 1000:
        tmp = d / "audio16k.part.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(d / "video.mp4"), "-vn", "-ac", "1", "-ar", "16000", str(tmp)])
        tmp.replace(wav)
    return wav

def _audio(d):
    wav = _ensure_wav(d)
    import wave
    with wave.open(str(wav)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

def transcribe(d, lang, model_name, prompt=None, beam=5, regions=None):
    """Full transcription, or (regions=[(s,e),...]) only those parts, merged into coarse words from YouTube subtitles."""
    tj = d / f"transcript_{model_name}.json"
    if tj.exists():
        c = json.loads(tj.read_text(encoding="utf-8")); c["words"] = _drop_phrase_loops(c["words"])
        if not c.get("regions") or regions is None or all(any(a >= x - 1 and b <= y + 1 for x, y in c["regions"]) for a, b in regions): return c
    log(f"transcribe model={model_name} lang={lang} beam={beam} " + (f"regions={len(regions)} ({sum(b-a for a,b in regions)/60:.1f} min)" if regions else "full"))
    audio = _audio(d); m = load_whisper(model_name)
    if not regions:
        words, L = _run_whisper(m, audio, lang, beam, prompt)
        res = {"language": L, "words": words}
    else:
        base = json.loads((d / "autosubs.json").read_text(encoding="utf-8")); L = base["language"] if lang == "auto" else lang
        fine = []
        for a, b in regions:
            w, _ = _run_whisper(m, audio[int(a * 16000): int(b * 16000)], L, beam, prompt, offset=a)
            fine += [x for x in w if a + 0.3 <= x["s"] <= b - 0.3]  # drop cut words at region edges
        cov = lambda t: any(a + 0.3 <= t <= b - 0.3 for a, b in regions)
        res = {"language": L, "regions": regions, "words": sorted([x for x in base["words"] if not cov(x["s"])] + fine, key=lambda x: x["s"])}
    tj.write_text(encoding="utf-8", data=json.dumps(res, ensure_ascii=False))
    return res

def candidate_regions(d, words, lang, heat, n, dmin, dmax, pad=8.0):
    """Pick ~2n best windows on coarse (YouTube) words, return merged padded regions to re-transcribe."""
    loud, hop = loudness_curve(d)
    ms = find_moments(words, lang, loud, hop, heat, n + 3, dmin, dmax)
    R = sorted((max(0, m["start"] - pad), m["end"] + pad) for m in ms); out = []
    for a, b in R:
        if out and a <= out[-1][1] + 2: out[-1] = (out[-1][0], max(out[-1][1], b))
        else: out.append((a, b))
    return [(round(a, 2), round(b, 2)) for a, b in out]

def loudness_curve(d, hop=0.5):
    wav = _ensure_wav(d)
    import wave
    with wave.open(str(wav)) as f:
        x = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = int(16000 * hop); k = len(x) // n
    rms = np.sqrt((x[:k*n].reshape(k, n) ** 2).mean(1) + 1e-9)
    db = 20 * np.log10(rms)
    return (db - np.median(db)) / (db.std() + 1e-6), hop

# ---------------------------------------------------------------- moment finding
HOOK = {
 "en": "why how what secret truth mistake never always nobody everyone imagine biggest worst best actually crazy insane problem wrong stop most important reason here's listen realized changed shocking surprising truth lie money million billion die dead love hate fear afraid",
 "pl": "dlaczego jak co sekret prawda błąd nigdy zawsze nikt każdy wyobraź największy najgorszy najlepszy naprawdę szalone problem źle stop najważniejsze powód słuchaj zrozumiałem zmieniło szok kłamstwo pieniądze milion miliard śmierć kocham nienawidzę strach boję",
}
EMO = {
 "en": "amazing incredible terrible horrible love hate scared angry funny laugh cried shocked unbelievable wow huge massive powerful beautiful painful hard fail failed success win lost",
 "pl": "niesamowite niewiarygodne straszne okropne kocham nienawidzę przerażony wściekły śmieszne płakałem szok wow ogromne potężne piękne bolesne trudne porażka sukces wygrałem straciłem kurde",
}
WEAK_START = {"and","but","so","because","or","also","then","which","that","i","um","uh","yeah","like",
              "i","a","ale","więc","bo","że","no","to","też","który","która","czyli","eee","yyy"}
STOP = set("the a an and or but so to of in on at for with is are was were be been it this that i you we they he she my your our their not do did have has had just like yeah um uh me him her them as from by what when there here can will would about if all one get got know think really very".split()) | \
       set("i w na z że to się nie jest jak do co ale o a tak po za od dla czy jest być są był była mi mnie ja ty my oni ona on go jej ich tego tym ten ta te no też już tylko bardzo może więc bo".split())

EXTRA_STOP = set("then those there's there these they're that's what's it's i've i'm don't didn't going gonna because about would could should which where while every something thing things people really actually right yeah okay jest żeby jakby właśnie wtedy który która które tutaj teraz bardzo jeszcze wszystko trochę mówi".split())
_LEAD = STOP | EXTRA_STOP | WEAK_START
_CONJ = {"aby", "że", "ze", "and", "that", "but", "or", "nor", "because", "which", "oraz", "albo"}
# Promo / end-card / URL. A window that matches is rejected, not merely scored down.
PROMO = re.compile(
    r"follow\s+(?:me|us)|subscri\w*|subskry\w*|obserwuj\w*|"
    r"link\s+in\s+(?:the\s+)?description|link\s+w\s+opisie|"
    r"zostaw\w*\s+(?:lajk\w*|łapk\w*|sub\w*)|"
    r"kod\s+rabatow\w*|(?:promo|discount)\s+code|sponsor\w*|"
    r"on\s+instagram\w*|na\s+instagram\w*|on\s+tiktok|na\s+tiktok\w*|"
    r"on\s+facebook\w*|na\s+facebook\w*|"
    r"@[\w.]{3,}|\bwww\b|\bhttps?\b|https?://|"
    r"\.(?:com|pl|net|org|gg|youtube)\b|youtube\.com",
    re.I)
MIN_RAW = -2.0  # below this (about 3/10) a window is not used to fill the requested clip count

def _latin(c):
    o = ord(c)
    return c.isascii() or 0x00C0 <= o <= 0x024F

def _junk_token(w):
    """Emoji, URLs, dotted garbage (.ploplesy) and mixed-script tokens are not speech."""
    raw = (w or "").strip().strip("\"'“”„«»()[]")
    if not raw: return True
    body = re.sub(r"[.!?…,;:]+$", "", raw).lstrip("-–—")
    if "." in body: return True
    letters = [c for c in body if c.isalpha()]
    if not letters or any(not _latin(c) for c in letters): return True
    low = re.sub(r"[^\w]", "", body.lower())
    return low in {"www", "http", "https", "com", "youtube"}

def _median_wps(words, bucket=20.0):
    if not words: return 2.0
    counts = {}
    for w in words: b = int(w["s"] // bucket); counts[b] = counts.get(b, 0) + 1
    rates = sorted(c / bucket for c in counts.values())
    return rates[len(rates) // 2] if rates else 2.0

def _repetitive(toks):
    """Same phrase three times and covering most of the window, or very few unique words."""
    n = len(toks)
    if n >= 8 and len(set(toks)) / n < 0.4: return True
    for k in range(2, 6):
        if k * 3 > n: break
        i = 0
        while i + k * 3 <= n:
            g = toks[i:i + k]
            if any(g) and toks[i:i + k * 3] == g * 3:
                j = i + k * 3
                while j + k <= n and toks[j:j + k] == g: j += k
                if (j - i) / n >= 0.4: return True
            i += 1
    return False

def sentences(words):
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        gap = words[i+1]["s"] - w["e"] if i + 1 < len(words) else 9
        nxt = words[i+1]["w"] if i + 1 < len(words) else ""
        punct = re.search(r"[.!?…]$", w["w"]) and (gap >= 0.12 or nxt[:1].isupper())
        if punct or gap > 0.8 or (len(cur) > 60 and gap > 0.35):
            out.append(cur); cur = []
    if cur: out.append(cur)
    res = []
    for c in out:
        i1 = c[-1]; gap = words[i1+1]["s"] - words[i1]["e"] if i1 + 1 < len(words) else 9
        clean = bool(re.search(r"[.!?…]$", words[i1]["w"])) and gap >= 0.12 or gap >= 0.5
        res.append({"i0": c[0], "i1": i1, "s": words[c[0]]["s"], "e": words[i1]["e"], "end_ok": clean,
                    "text": " ".join(words[j]["w"] for j in c)})
    for k, x in enumerate(res):
        x["start_ok"] = k == 0 or res[k-1]["end_ok"]
    return res

def norm(t): return re.sub(r"[^\wąćęłńóśźż']", "", t.lower())

def score_window(sents, a, b, words, lang, loud, hop, heat, dur_total, rate=None):
    L = "pl" if lang == "pl" else "en"
    hook_set, emo_set = set(HOOK[L].split()) | set(HOOK["en"].split()), set(EMO[L].split())
    first = sents[a]; s, e = first["s"], sents[b]["e"]; D = e - s
    toks = [norm(words[j]["w"]) for j in range(first["i0"], sents[b]["i1"] + 1)]
    ftoks = [norm(words[j]["w"]) for j in range(first["i0"], first["i1"] + 1)]
    sc, why = 0.0, []
    bare = [t for t in toks if t]
    win = " ".join(sents[k]["text"] for k in range(a, b + 1))
    if D >= 12:
        wps = len(bare) / D; med = _median_wps(words) if rate is None else rate
        if wps < 1.0 or wps < 0.4 * med: return -99.0, ["sparse"]  # not enough speech to be a clip
    if _repetitive(bare): return -99.0, ["loop"]
    if PROMO.search(win): return -99.0, ["promo"]
    h = sum(t in hook_set for t in ftoks) + 1.5 * first["text"].strip().endswith("?") + 0.8 * any(t.isdigit() for t in ftoks)
    h += 0.6 * any(t in ("you", "your", "ty", "ciebie", "twój", "wy") for t in ftoks)
    sc += min(h, 4) * 1.2; why.append(f"hook={h:.1f}")
    if ftoks and ftoks[0] in WEAK_START: sc -= 1.5; why.append("weak_start")
    if len(ftoks) < 3 or len(ftoks) > 35: sc -= 0.7
    emo = sum(t in emo_set or t in hook_set for t in toks) / max(len(toks), 1) * 30
    sc += min(emo, 3); why.append(f"emo={emo:.1f}")
    q = sum(sents[k]["text"].strip().endswith("?") for k in range(a, b + 1)); ex = sum(sents[k]["text"].strip().endswith("!") for k in range(a, b + 1))
    sc += min(q, 2) * 0.4 + min(ex, 3) * 0.4
    wps = len(toks) / D; sc += max(0, min(wps - 2.0, 1.5)); why.append(f"wps={wps:.1f}")
    if not re.search(r"[.!?…]$", sents[b]["text"]): sc -= 1.0; why.append("no_end")
    nxt = sents[b]["i1"] + 1
    if nxt < len(words) and words[nxt]["s"] - words[sents[b]["i1"]]["e"] < 0.25: sc -= 0.8; why.append("tight_end")
    lz = loud[int(s / hop): int(e / hop) + 1]
    if len(lz): lv = float(np.percentile(lz, 90)); sc += max(-1, min(lv, 2)) * 0.6; why.append(f"loud={lv:.1f}")
    if heat:
        hv = [x["value"] for x in heat if x["end_time"] > s and x["start_time"] < e]
        if hv: m = float(np.mean(hv)); sc += m * 6; why.append(f"heat={m:.2f}")
    sc += -abs(D - 38) / 40  # prefer ~30-45s
    if s < 8: sc -= 0.5  # intro (opening seconds)
    return sc, why

def clip_bounds(words, i0, i1, head=0.15, tail=0.4):
    """Start just before word i0 (never inside previous word), end 0.3-0.5s after word i1 but never into word i1+1."""
    s = words[i0]["s"] - head
    if i0 > 0: s = max(s, min(words[i0-1]["e"], words[i0]["s"]))
    e = words[i1]["e"] + tail
    if i1 + 1 < len(words): e = min(e, words[i1+1]["s"] - 0.08)
    e = max(e, words[i1]["e"] + 0.05)
    return round(max(0, min(s, words[i0]["s"])), 3), round(e, 3)

def snap_bounds(words, s, e):
    """For manual nudges: never start or end inside a word."""
    for w in words:
        if w["s"] < s < w["e"]: s = w["s"]
        if w["s"] < e < w["e"]: e = w["e"] + 0.1
    inside = [i for i, w in enumerate(words) if w["s"] >= s - 0.01 and w["e"] <= e + 0.01]
    if inside:
        cs, ce = clip_bounds(words, inside[0], inside[-1])
        s = min(s, cs) if s > words[inside[0]]["s"] - 0.3 else s
        e = max(e, words[inside[-1]]["e"] + 0.05)
        if inside[-1] + 1 < len(words): e = min(e, max(words[inside[-1]+1]["s"] - 0.05, words[inside[-1]]["e"] + 0.05))
    return round(max(0, s), 3), round(e, 3)

def apply_edits(words, edits):
    """edits: {"<start_time>": {"w": text, "del": bool}} -> caption words."""
    if not edits: return words
    out = []
    for w in words:
        ed = edits.get(f"{w['s']:.3f}")
        if ed:
            if ed.get("del") or not ed.get("w", "").strip(): continue
            w = dict(w, w=ed["w"].strip())
        out.append(w)
    return out

def clip_rating(sc):
    """Raw heuristic sum -> integer 1–10 (5 is a neutral clip)."""
    return int(max(1, min(10, round(5 + 0.6 * sc))))

def find_moments(words, lang, loud, hop, heat, n, dmin=20, dmax=60, regions=None):
    sents = sentences(words); dur_total = words[-1]["e"]; rate = _median_wps(words)
    inreg = lambda s, e: not regions or any(s >= a + 0.3 and e <= b - 0.3 for a, b in regions)
    cands = []
    for a in range(len(sents)):
        for b in range(a, len(sents)):
            D = sents[b]["e"] - sents[a]["s"]
            if D > dmax: break
            if D < dmin: continue
            if not (sents[a]["start_ok"] and sents[b]["end_ok"]): continue
            if not inreg(sents[a]["s"], sents[b]["e"]): continue
            sc, why = score_window(sents, a, b, words, lang, loud, hop, heat, dur_total, rate)
            if sc <= -50 or sc < MIN_RAW: continue  # failed filter, or too weak to pad the list
            cands.append((sc, a, b, why))
    cands.sort(key=lambda c: -c[0]); picked = []
    for c in cands:
        s, e = sents[c[1]]["s"], sents[c[2]]["e"]
        if all(e <= p["start"] - 1 or s >= p["end"] + 1 for p in picked):
            parts = [sents[k]["text"] for k in range(c[1], c[2] + 1)]
            if not clip_title(parts, lang): continue  # no real sentence: skip, don't invent a title
            i0, i1 = sents[c[1]]["i0"], sents[c[2]]["i1"]
            bs, be = clip_bounds(words, i0, i1)
            picked.append({"start": bs, "end": be, "score": clip_rating(c[0]), "score_raw": round(c[0], 2),
                           "why": c[3], "hook": parts[0], "text": " ".join(parts), "sents": parts})
        if len(picked) >= n: break
    return picked

def _cap_first(s):
    return (s[:1].upper() + s[1:]) if s else s

def sent_strength(text, lang):
    """Same hook/emotion cues as score_window, plus a bonus for a finished sentence."""
    L = "pl" if lang == "pl" else "en"
    hook_set, emo_set = set(HOOK[L].split()) | set(HOOK["en"].split()), set(EMO[L].split())
    raw = re.sub(r"\s+", " ", text or "").strip(" \"'“”„«»")
    toks = [norm(w) for w in raw.split() if norm(w)]
    if not toks: return -99.0
    h = sum(t in hook_set for t in toks) + 1.5 * raw.endswith("?") + 0.8 * any(any(c.isdigit() for c in t) for t in toks)
    h += 0.6 * any(t in ("you", "your", "ty", "ciebie", "twój", "twoj", "wy") for t in toks)
    emo = sum(t in emo_set or t in hook_set for t in toks) / max(len(toks), 1) * 30
    sc = min(h, 4) * 1.2 + min(emo, 3)
    if re.search(r"[.!?…]$", raw): sc += 1.5
    if toks[0] in WEAK_START: sc -= 1.5
    content = [t for t in toks if t not in _LEAD and len(t) > 2]
    if len(toks) < 3 or len(content) < 2: sc -= 4
    if PROMO.search(raw): sc -= 3
    return sc

def _tiny(w):
    b = norm(w)
    return len(b) < 3 and not any(c.isdigit() for c in b)

def _title_ok(t):
    if not t: return False
    if t.lower().strip(" .") in {"best spoken moment", "najlepszy moment filmu"}: return False
    if re.search(r"[,;:]\s*$", t): return False
    if norm(t.split()[-1]) in _CONJ: return False
    if PROMO.search(t): return False
    return True

def clean_title(text):
    """Strip fillers, URLs and cut-off tails; keep a 3-word / 15-char phrase, cap ~60."""
    words = [w for w in re.sub(r"\s+", " ", text or "").strip().split() if not _junk_token(w)]
    words = [re.sub(r"[,;:，]+$", "", w).strip(" \t\"'“”„«»") for w in words]
    words = [w for w in words if w]
    lead = lambda w: (not norm(w)) or norm(w) in _LEAD or norm(w) in _CONJ or _tiny(w)
    while words and lead(words[0]): words.pop(0)
    while len(words) > 3 and lead(words[-1]): words.pop()
    while words and norm(words[-1]) in _CONJ: words.pop()
    phrase = re.sub(r"[\s,;:–—\-…]+$", "", " ".join(words)).strip()
    if len(phrase) > 60:
        cut = phrase[:60]; sp = cut.rfind(" ")
        phrase = (cut[:sp] if sp >= 15 else cut).strip(" \t,;:–—-")
        bits = phrase.split()
        while len(bits) > 3 and lead(bits[-1]): bits.pop()
        while bits and norm(bits[-1]) in _CONJ: bits.pop()
        phrase = " ".join(bits)
    toks = [norm(w) for w in phrase.split() if norm(w)]
    if len(toks) < 3 or len(phrase) < 15 or len(toks) == 1: return ""
    if all(t in _LEAD or t in _CONJ for t in toks): return ""
    return _cap_first(phrase)

def _pick_title(parts, lang):
    parts = [re.sub(r"\s+", " ", p).strip() for p in parts if p and str(p).strip()]
    order = sorted(range(len(parts)), key=lambda i: (sent_strength(parts[i], lang), -i), reverse=True)
    for i in order:
        t = clean_title(parts[i])
        if _title_ok(t): return t, parts[i]
    content = [w.strip(".,;:!?…\"'“”„") for p in parts for w in p.split()
               if not _junk_token(w) and len(norm(w)) > 2 and norm(w) not in _LEAD and norm(w) not in _CONJ]
    t = clean_title(" ".join(w for w in content if w))
    if _title_ok(t): return t, " ".join(content)
    return "", ""

def clip_title(parts, lang="en"):
    """Meaningful phrase from the clip's sentences. Never a lone stopword."""
    return _pick_title(parts, lang)[0]

def _clip_parts(m):
    parts = [s for s in (m.pop("sents", None) or []) if str(s).strip()]
    if parts: return parts
    blob = (m.get("text") or m.get("hook") or "").strip()
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", blob) if p.strip()]
    return parts or ([blob] if blob else [])

def title_and_tags(m, lang):
    title, src = _pick_title(_clip_parts(m), lang)
    if not title: return "", "", []
    m["hook"] = src or title
    toks = [norm(t) for t in m["text"].split() if not _junk_token(t)]
    freq = {}
    for t in toks:
        if len(t) > 4 and t not in STOP and t not in EXTRA_STOP and not t.isdigit(): freq[t] = freq.get(t, 0) + 1
    kws = [k for k, _ in sorted(freq.items(), key=lambda x: -x[1])[:4]]
    base = ["#shorts", "#viral", "#fyp"] + (["#polska", "#dlaciebie"] if lang == "pl" else ["#podcast"])
    tags = ["#" + k for k in kws] + base
    return title, f"{title} {' '.join(tags)}", tags

# ---------------------------------------------------------------- optional LLM rerank
def llm_available():
    for k in ["OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"]:
        if os.environ.get(k): return k
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1); return "OLLAMA"
    except Exception: return None

def llm_titles(moments, lang):
    """Jeśli dostępny jest darmowy lokalny Ollama (lub klucz w env), poprawia tytuły/opisy. Inaczej no-op."""
    kind = llm_available()
    if not kind: return False
    prompt = ("For each numbered short-video clip transcript, write a punchy title (<=8 words) and a social caption with 4 hashtags, "
              f"in language '{lang}'. Return JSON list of objects {{i,title,caption}}.\n\n" +
              "\n".join(f"{i}: {m['text'][:1200]}" for i, m in enumerate(moments)))
    try:
        if kind == "OLLAMA":
            model = os.environ.get("CLIP_OLLAMA_MODEL", "qwen2.5:7b")
            req = urllib.request.Request("http://127.0.0.1:11434/api/generate", json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode(), {"Content-Type": "application/json"})
            txt = json.loads(urllib.request.urlopen(req, timeout=600).read())["response"]
        else:
            base = {"OPENAI_API_KEY": "https://api.openai.com/v1", "GROQ_API_KEY": "https://api.groq.com/openai/v1", "OPENROUTER_API_KEY": "https://openrouter.ai/api/v1"}[kind]
            model = os.environ.get("CLIP_LLM_MODEL", {"OPENAI_API_KEY": "gpt-4o-mini", "GROQ_API_KEY": "llama-3.3-70b-versatile", "OPENROUTER_API_KEY": "meta-llama/llama-3.3-70b-instruct:free"}[kind])
            req = urllib.request.Request(base + "/chat/completions", json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[kind]})
            txt = json.loads(urllib.request.urlopen(req, timeout=300).read())["choices"][0]["message"]["content"]
        data = json.loads(re.search(r"[\[{].*[\]}]", txt, re.S).group(0))
        if isinstance(data, dict): data = next(v for v in data.values() if isinstance(v, list))
        for o in data:
            m = moments[int(o["i"])]; m["title"] = o["title"]; m["caption"] = o["caption"]
        return True
    except Exception as e:
        log("LLM fail, using heuristics:", e); return False

# ---------------------------------------------------------------- edit
def keep_intervals(words, s, e, max_gap=2.0, pad=0.35):
    """Drop a pause only when it is longer than max_gap, leaving `pad` on each side.
    YouTube autosub words (p<=0.5) have guessed ends, so a gap they touch is kept.
    If the trim would leave under 75% of the window, return the window unchanged."""
    ws = [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05]
    if not ws: return [(s, e)]
    iv = [[s, ws[0]["e"]]]
    for a, b in zip(ws, ws[1:]):
        if b["s"] - a["e"] > max_gap + 2 * pad and min(a.get("p", 1), b.get("p", 1)) > 0.5:
            iv[-1][1] = a["e"] + pad; iv.append([b["s"] - pad, b["e"]])
        else: iv[-1][1] = b["e"]
    iv[-1][1] = e
    if sum(b - a for a, b in iv) < 0.75 * (e - s): return [(s, e)]
    return [(round(a, 3), round(b, 3)) for a, b in iv]

def src_to_out(t, iv):
    acc = 0
    for a, b in iv:
        if t < a: return acc
        if t <= b: return acc + t - a
        acc += b - a
    return acc

def ass_time(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def make_ass(words, iv, path, font, size=112, hl="&H0000E5FF", words_per=3):
    ws = [w for w in words if not _junk_token(w["w"]) and any(w["s"] < b and w["e"] > a for a, b in iv)]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,7,4,2,70,70,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    # group into chunks (break on punctuation / pause)
    chunks, cur = [], []
    for i, w in enumerate(ws):
        cur.append(w)
        nxt = ws[i+1] if i + 1 < len(ws) else None
        if len(cur) >= words_per or (nxt and sum(len(x["w"]) + 1 for x in cur) + len(nxt["w"]) > 24) or re.search(r"[.!?,;:]$", w["w"]) or (nxt and nxt["s"] - w["e"] > 0.5) or not nxt:
            chunks.append(cur); cur = []
    for ch in chunks:
        for k, w in enumerate(ch):
            st = src_to_out(w["s"], iv); en = src_to_out(ch[k+1]["s"], iv) if k + 1 < len(ch) else src_to_out(w["e"], iv) + 0.15
            if en - st < 0.05: en = st + 0.05
            parts = []
            for j, x in enumerate(ch):
                t = re.sub(r"[{}\\]", "", x["w"]).upper()
                t = re.sub(r"[,.;:]$", "", t).lstrip("-")
                parts.append(f"{{\\c{hl}\\fscx108\\fscy108}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if j == k else t)
            pop = "{\\fad(0,0)}"
            lines.append([st, en, f"{pop}{' '.join(parts)}"])
    for k in range(len(lines) - 1):
        lines[k][1] = min(lines[k][1], lines[k+1][0])
    lines = [f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,{tx}" for st, en, tx in lines if en - st > 0.01]
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")

def probe(path):
    j = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", str(path)]))
    st = j["streams"][0]; num, den = map(int, st["r_frame_rate"].split("/"))
    return st["width"], st["height"], num / den

def read_frames(src, s, D, W, H):
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-ss", f"{s:.3f}", "-i", str(src), "-t", f"{D:.3f}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE, bufsize=W*H*3*4)
    n = W * H * 3
    while True:
        b = p.stdout.read(n)
        if len(b) < n: break
        yield np.frombuffer(b, np.uint8).reshape(H, W, 3)
    p.wait()

def face_track(src, s, D, W, H, fps, step_s=0.2):
    import cv2
    sc = 640 / W; dw, dh = 640, int(H * sc) // 2 * 2
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (dw, dh), 0.6, 0.3, 5000)
    xs, ts, hits, total = [], [], 0, 0
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-ss", f"{s:.3f}", "-i", str(src), "-t", f"{D:.3f}", "-an",
                          "-vf", f"fps={1/step_s:g},scale={dw}:{dh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    n = dw * dh * 3; i = -1
    while True:
        b = p.stdout.read(n)
        if len(b) < n: break
        i += 1; total += 1; f = np.frombuffer(b, np.uint8).reshape(dh, dw, 3)
        _, faces = det.detect(f)
        t = i * step_s
        if faces is not None and len(faces):
            # largest face, weighted by confidence
            best = max(faces, key=lambda r: r[2] * r[3] * r[-1])
            xs.append((best[0] + best[2] / 2) / sc); ts.append(t); hits += 1
    p.stdout.close(); p.wait()
    return np.array(ts), np.array(xs), hits / max(total, 1)

def smooth_path(ts, xs, nframes, fps, W, cw):
    t_all = np.arange(nframes) / fps
    if len(xs) == 0: return np.full(nframes, (W - cw) / 2)
    # median filter to kill outliers, then interpolate, deadzone + EMA
    k = 5; xm = np.array([np.median(xs[max(0, i-k//2): i+k//2+1]) for i in range(len(xs))])
    x = np.interp(t_all, ts, xm)
    out = np.empty_like(x); cur = x[0]; dead = cw * 0.08; alpha = 1 - math.exp(-1 / (fps * 0.35))
    target = cur
    for i, v in enumerate(x):
        if abs(v - target) > dead: target = v
        cur += (target - cur) * alpha; out[i] = cur
    return np.clip(out - cw / 2, 0, W - cw)

def render_clip(src, words, m, outp, font, tmp, cap_words=None):
    import cv2
    W, H, fps = probe(src)
    s, e = m["start"], m["end"]; iv = keep_intervals(words, s, e)
    D = e - s
    ts, xs, ratio = face_track(src, s, D, W, H, fps)
    mode = "face" if ratio >= 0.35 else "blur"
    if W / H < 0.7: mode = "native"
    cw = int(H * 9 / 16) // 2 * 2
    nframes = int(D * fps) + 2
    path = smooth_path(ts, xs, nframes, fps, W, cw) if mode == "face" else None
    # per-frame face presence: no face within 0.6s -> blurred-fit layout for that frame (slides, b-roll)
    if mode == "face":
        tf = np.arange(nframes) / fps
        near = np.min(np.abs(tf[:, None] - ts[None, :]), axis=1) if len(ts) else np.full(nframes, 9.0)
        has = near < 0.6
        # debounce: remove switches shorter than 0.8s
        k = int(fps * 0.8); hs = has.copy()
        i = 0
        while i < nframes:
            j = i
            while j < nframes and has[j] == has[i]: j += 1
            if j - i < k and i > 0: hs[i:j] = hs[i-1]
            i = j
        has = hs
    else:
        has = None
    ass = tmp / (outp.stem + ".ass"); make_ass(cap_words if cap_words is not None else words, iv, ass, font)
    # audio filter: select keep intervals
    sel = "+".join(f"between(t,{a-s:.3f},{b-s:.3f})" for a, b in iv)
    font_rel = Path(os.path.relpath(FONTS, tmp)).as_posix()  # e.g. ../../../app/fonts
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "1080x1920", "-r", f"{fps:.5f}", "-i", "-",
        "-ss", f"{s:.3f}", "-t", f"{D:.3f}", "-i", str(Path(src).resolve()),
        "-filter_complex", f"[0:v]ass='{ass.name}':fontsdir='{font_rel}'[v];[1:a]aselect='{sel}',asetpts=N/SR/TB,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(Path(outp).resolve())], stdin=subprocess.PIPE, cwd=str(tmp))
    for i, f in enumerate(read_frames(src, s, D, W, H)):
        t = s + i / fps
        if not any(a <= t <= b for a, b in iv): continue
        if mode == "face" and has[min(i, nframes-1)]:
            x0 = int(path[min(i, len(path)-1)]); fr = cv2.resize(f[:, x0:x0+cw], (1080, 1920), interpolation=cv2.INTER_LANCZOS4 if H < 1000 else cv2.INTER_LINEAR)
        elif mode == "native":
            fr = cv2.resize(f, (1080, 1920))
        else:  # blur-fit
            bg = cv2.resize(f[:, (W-cw)//2:(W-cw)//2+cw], (108, 192)); bg = cv2.resize(cv2.GaussianBlur(bg, (0, 0), 3), (1080, 1920)) * 0.6
            fh = int(1080 * H / W); fg = cv2.resize(f, (1080, fh)); fr = bg.astype(np.uint8)
            y0 = (1920 - fh) // 2 - 150; fr[y0:y0+fh] = fg
        enc.stdin.write(np.ascontiguousarray(fr).tobytes())
    enc.stdin.close(); enc.wait()
    if enc.returncode: raise RuntimeError("ffmpeg encode failed")
    return mode, ratio, iv

def duration(p):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(p)]))

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="clip", description="Darmowy auto-clipper YouTube -> pionowe shorty")
    ap.add_argument("url", nargs="?"); ap.add_argument("--rerender", help="video_id"); ap.add_argument("--clip", type=int); ap.add_argument("--n", type=int, default=5); ap.add_argument("--lang", default="auto")
    ap.add_argument("--model", default=os.environ.get("CLIP_WHISPER", "auto"), help="auto/tiny/base/small/medium/large-v3/turbo")
    ap.add_argument("--preset", default=os.environ.get("CLIP_PRESET", "zbalansowany"), choices=["szybki", "zbalansowany", "dokladny"])
    ap.add_argument("--font", default=os.environ.get("CLIP_FONT", "Poppins Black"))
    ap.add_argument("--min", type=float, default=25); ap.add_argument("--max", type=float, default=60)
    a = ap.parse_args(); T0 = time.time(); timings = {}
    if a.rerender: return rerender(a.rerender, a.clip, a.font)
    if not a.url: ap.error("podaj URL")
    WORK.mkdir(exist_ok=True)
    vid, d, info = download(a.url, WORK, a.lang); timings["download"] = time.time() - T0
    meta = json.loads((d / "info.json").read_text(encoding="utf-8")) if (d / "info.json").exists() else info
    t = time.time(); prompt = ((meta.get("title") or "") + ". " + (meta.get("description") or "")[:600]).strip(". ")
    if a.lang == "pl" or (a.lang == "auto" and re.search(r"[ąćęłńóśźż]", prompt, re.I)): prompt = "Rozmowa po polsku. " + prompt
    sys.path.insert(0, str(Path(__file__).resolve().parent)); import hw
    cfg = hw.pick(a.preset, a.model, os.environ.get("CLIP_DEVICE", "auto")); model = cfg["model"]; beam = cfg["beam"]
    log(f"preset={a.preset} tier={cfg['tier']} model={model} beam={beam} hw={cfg['hw']}")
    heat = meta.get("heatmap"); regions = None
    order = ["tiny", "base", "small", "medium", "large-v3"]
    for bm in reversed(order[order.index(model) if model in order else 0:]):  # reuse a cached full transcript of the same or better model
        if (d / f"transcript_{bm}.json").exists() and not json.loads((d / f"transcript_{bm}.json").read_text(encoding="utf-8")).get("regions"):
            if bm != model: log(f"reusing cached transcript {bm}")
            model = bm; break
    sub = d / "autosubs.json"
    if a.preset != "dokladny" and sub.exists() and os.environ.get("CLIP_HYBRID", "1") == "1":
        cw = json.loads(sub.read_text(encoding="utf-8")); L0 = cw["language"] if a.lang == "auto" else a.lang
        regions = candidate_regions(d, cw["words"], L0, heat, a.n, a.min, a.max)
        if sum(b - x for x, b in regions) > 0.7 * (meta.get("duration") or 1e9): regions = None  # short video: just do it all
        else: log(f"fast path: YouTube subtitles + Whisper only on {len(regions)} candidate regions")
    tr = transcribe(d, a.lang, model, prompt, beam, regions); timings["transcribe"] = time.time() - t
    a.model = model; regions = tr.get("regions")
    lang = tr["language"]; words = tr["words"]; log(f"lang={lang} words={len(words)}")
    loud, hop = loudness_curve(d)
    log("heatmap:", "yes" if heat else "no")
    moments = find_moments(words, lang, loud, hop, heat, a.n, a.min, a.max, regions)
    if lang == "pl":  # Polish spellcheck of caption words inside the chosen clips (auto-fix only confident cases)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent)); import spell
            t = time.time(); st = {"flagged": 0, "fixed": 0}
            for m in moments:
                r = spell.check_words([w for w in words if m["start"] - 0.5 <= w["s"] <= m["end"] + 0.5 and "sug" not in w])
                for k in st: st[k] += r.get(k, 0)
            fx = {w["asr"]: w["w"] for w in words if "asr" in w}
            for m in moments:
                for k in ("text", "hook"):
                    m[k] = " ".join(fx.get(t, t) for t in m[k].split())
                if m.get("sents"):
                    m["sents"] = [" ".join(fx.get(t, t) for t in s.split()) for s in m["sents"]]
            log(f"spellcheck flagged={st['flagged']} fixed={st['fixed']} in {time.time()-t:.1f}s")
            tjp = d / f"transcript_{a.model}.json"; tr["words"] = words
            tjp.write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log("spellcheck skipped:", e)
    kept = []
    for m in moments:
        m["title"], m["caption"], m["tags"] = title_and_tags(m, lang)
        if m["title"]: kept.append(m)
        else: log("skip clip without a title", round(m["start"], 1))
    moments = kept
    used_llm = llm_titles(moments, lang)
    od = OUT / vid; od.mkdir(parents=True, exist_ok=True); tmp = d / "tmp"; tmp.mkdir(exist_ok=True)
    t = time.time()
    from concurrent.futures import ThreadPoolExecutor
    def _r(im):
        i, m = im; outp = od / f"clip_{i:02d}.mp4"; log(f"render {i}/{len(moments)} {m['start']}-{m['end']} score={m['score']}")
        mode, ratio, iv = render_clip(d / "video.mp4", words, m, outp, a.font, tmp)
        m.update({"file": str(outp), "crop_mode": mode, "face_ratio": round(ratio, 2), "duration": round(duration(outp), 2),
                  "removed_silence_s": round((m["end"] - m["start"]) - sum(b - x for x, b in iv), 2)})
    par = 1 if (os.cpu_count() or 2) < 6 else 2
    with ThreadPoolExecutor(par) as ex: list(ex.map(_r, enumerate(moments, 1)))
    timings["render"] = time.time() - t; timings["total"] = time.time() - T0
    summ = {"video_id": vid, "title": meta.get("title"), "url": a.url, "language": lang, "whisper_model": a.model, "preset": a.preset, "hybrid": bool(regions),
            "heatmap_used": bool(heat), "llm_used": used_llm, "timings_s": {k: round(v, 1) for k, v in timings.items()}, "clips": moments}
    write_summary(od, summ)
    print(od)

def write_summary(od, summ):
    (od / "summary.json").write_text(encoding="utf-8", data=json.dumps(summ, ensure_ascii=False, indent=2))
    moments = summ["clips"]
    md = [f"# {summ.get('title')}\n", f"Źródło: {summ.get('url')} · język: {summ.get('language')}\n"]
    for i, m in enumerate(moments, 1):
        sc, raw = m.get("score"), m.get("score_raw")
        rating = int(sc) if isinstance(sc, (int, float)) and not isinstance(sc, bool) and float(sc).is_integer() and 1 <= sc <= 10 else None
        if rating is None:
            base = raw if raw is not None else sc
            rating, raw = clip_rating(base), round(float(base), 2)
        bit = f"ocena {rating}/10" + (f", raw {raw}" if raw is not None else "")
        md.append(f"## {i}. {m['title']}  ({bit})\n- plik: `{Path(m['file']).name}` · {ass_time(m['start'])}–{ass_time(m['end'])} · {m['duration']}s · crop: {m['crop_mode']}\n- opis: {m['caption']}\n- hook: „{m['hook']}”\n")
    (od / "summary.md").write_text(encoding="utf-8", data="\n".join(md))

def rerender(vid, n, font):
    od = OUT / vid; d = WORK / vid
    summ = json.loads((od / "summary.json").read_text(encoding="utf-8")); m = summ["clips"][n - 1]
    words = json.loads((d / f"transcript_{summ['whisper_model']}.json").read_text(encoding="utf-8"))["words"]
    m["start"], m["end"] = snap_bounds(words, float(m["start"]), float(m["end"]))
    cap = apply_edits(words, m.get("edits", {}).get("words"))
    final = od / f"clip_{n:02d}.mp4"; tmpout = od / f".clip_{n:02d}.new.mp4"
    tmp = d / "tmp"; tmp.mkdir(exist_ok=True)
    log(f"render 1/1 {m['start']}-{m['end']} (rerender clip {n})")
    mode, ratio, iv = render_clip(d / "video.mp4", words, m, tmpout, font, tmp, cap)
    os.replace(tmpout, final)
    m.update({"file": str(final), "crop_mode": mode, "duration": round(duration(final), 2), "ver": int(time.time()),
              "removed_silence_s": round((m["end"] - m["start"]) - sum(b - x for x, b in iv), 2)})
    write_summary(od, summ)
    print(od)

if __name__ == "__main__":
    main()
