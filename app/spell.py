"""Polish spellcheck for Whisper captions (hunspell pl_PL via spylls + 50k frequency list).
Flags words missing from the dictionary, suggests close frequent words, auto-fixes only very confident cases."""
import re, unicodedata
from pathlib import Path

D = Path(__file__).resolve().parent / "dict"
_dic = None; _freq = None; _bylen = None
VER = 1

def _load():
    global _dic, _freq, _bylen
    if _dic is None:
        from spylls.hunspell import Dictionary
        _dic = Dictionary.from_files(str(D / "pl_PL"))
        _freq = {}
        for line in (D / "pl_50k.txt").read_text(encoding="utf-8").splitlines():
            p = line.split()
            if len(p) == 2 and p[0].isalpha(): _freq[p[0]] = int(p[1])
        _bylen = {}
        for w in _freq: _bylen.setdefault(len(w), []).append(w)

def phon(w):
    """rough Polish phonetic key (rz=ż, ó=u, ch=h, voicing pairs)"""
    w = w.lower()
    for a, b in [("rz", "ż"), ("ó", "u"), ("ch", "h"), ("ą", "o"), ("ę", "e"), ("dż", "cz"), ("dź", "ć"), ("dz", "c"), ("w", "f"), ("z", "s"), ("ź", "ś"), ("ż", "sz"), ("b", "p"), ("d", "t"), ("g", "k"), ("y", "i")]:
        w = w.replace(a, b)
    return w

def dist(a, b, maxd=2):
    """Damerau-Levenshtein (optimal string alignment) with early exit"""
    if abs(len(a) - len(b)) > maxd: return maxd + 1
    prev2 = None; prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b); best = i
        for j in range(1, len(b) + 1):
            c = 0 if a[i-1] == b[j-1] else 1
            cur[j] = min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + c)
            if i > 1 and j > 1 and a[i-1] == b[j-2] and a[i-2] == b[j-1]: cur[j] = min(cur[j], prev2[j-2] + 1)
            best = min(best, cur[j])
        if best > maxd: return maxd + 1
        prev2, prev = prev, cur
    return prev[-1]

def known(w):
    _load(); lw = w.lower()
    return bool(_dic.lookup(w) or _dic.lookup(lw))

def candidates(w, n=3):
    _load(); lw = w.lower(); pw = phon(lw); out = {}
    for L in range(len(lw) - 2, len(lw) + 3):
        for c in _bylen.get(L, ()):
            d = dist(lw, c)
            if d <= 2 and _dic.lookup(c):  # freq list (subtitles) contains typos: keep only real words
                pd = dist(pw, phon(c))
                out[c] = (d + 0.6 * min(pd, 3)) - min(_freq[c], 200000) ** 0.25 / 12
    if out: return sorted(out.items(), key=lambda x: x[1])[:n]
    for s in _dic.suggest(lw):  # hunspell suggestions (rarer words; slow ~1 s) only if nothing frequent is close
        if " " in s or "-" in s: continue
        s = s.lower(); d = dist(lw, s)
        if d <= 2 and s not in out: out[s] = d + 0.6 * min(dist(pw, phon(s)), 3)
        if len(out) > 40: break
    return sorted(out.items(), key=lambda x: x[1])[:n]

def _case(src, dst):
    if src.isupper() and len(src) > 1: return dst.upper()
    if src[:1].isupper(): return dst[:1].upper() + dst[1:]
    return dst

SPLIT = re.compile(r"^([^\w]*)([\w]+(?:-[\w]+)*)([^\w]*)$", re.U)

def check_words(words, auto=True):
    """words: [{'w','s','e','p'}]. Adds 'sug' (list) for flagged words; auto-fixes confident ones (keeps 'asr' original)."""
    try: _load()
    except Exception as e: return {"error": repr(e)}
    fixed = flagged = 0
    for i, w in enumerate(words):
        m = SPLIT.match(w["w"])
        if not m: continue
        pre, core, post = m.groups()
        if len(core) < 3 or core.isdigit() or any(ch.isdigit() for ch in core) or known(core): continue
        # likely proper noun: capitalised not at sentence start
        prevw = words[i-1]["w"] if i else "."
        if core[:1].isupper() and not prevw.endswith((".", "!", "?")) and not core.isupper(): continue
        c = candidates(core)
        if not c: continue
        flagged += 1; w["sug"] = [_case(core, x) for x, _ in c]
        best, bs = c[0]; second = c[1][1] if len(c) > 1 else 99
        if auto and w.get("p", 1) < 0.6 and bs < 2.6 and second - bs >= 0.8 and best in _freq and _freq[best] >= 50:
            w["asr"] = w["w"]; w["w"] = pre + _case(core, best) + post; fixed += 1
    return {"flagged": flagged, "fixed": fixed}

if __name__ == "__main__":
    import sys, time
    t = time.time(); _load(); print("load", round(time.time() - t, 1))
    for x in sys.argv[1:] or ["rysmawiasz", "RYSMAWIASZ", "przepraszm", "Nawrocka", "spoko"]:
        t = time.time(); print(x, known(x), candidates(x), round(time.time() - t, 2))
    ws = [{"w": "A", "p": .9}, {"w": "jak", "p": .9}, {"w": "rysmawiasz?", "p": .3}]
    print(check_words(ws), ws)
