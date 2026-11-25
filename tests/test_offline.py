#!/usr/bin/env python3
"""Offline checks for silence cuts, captions, clip rating, titles, and finder presets.

No YouTube and no network. Run: python tests/test_offline.py
"""
import json, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import finder
from main import (
    STOP, _TRAIL, _drop_phrase_loops, _drop_repeat_segs, ass_time, autosub_key,
    clean_speech, clear_clip_outputs, clip_rating, clip_title, find_moments, _junk_token, MIN_RAW,
    keep_intervals, make_ass, score_window, sentences, src_to_out, title_and_tags,
    write_summary,
)


def word(text, s, e, p=0.9):
    return {"w": text, "s": s, "e": e, "p": p}


def test_gap_1_2_not_cut():
    # 1.2s is under the cut threshold (max_gap 2.0 + 2*pad 0.35 = 2.7s).
    # Window is long enough that the old 1.0s threshold would have kept the cut
    # (the 75% safety would not have put it back).
    words = [word("aaa", 1.0, 2.0), word("bbb", 3.2, 4.0), word("ccc", 4.2, 24.0)]
    iv = keep_intervals(words, 0.0, 26.3)
    kept = sum(b - a for a, b in iv)
    print(f"  1.2s gap in 26.3s window -> {iv} kept={kept:.2f}s")
    assert iv == [(0.0, 26.3)], iv
    assert abs(kept - 26.3) < 1e-6


def test_gap_3_5_cut_with_pad():
    words = [word("aaa", 1.0, 2.0), word("bbb", 5.5, 6.5), word("ccc", 7.0, 16.0)]
    iv = keep_intervals(words, 0.0, 20.0)
    kept = sum(b - a for a, b in iv)
    print(f"  3.5s gap -> {iv} kept={kept:.2f}s pads=({iv[0][1] - 2.0:.3f}, {5.5 - iv[1][0]:.3f})")
    assert iv == [(0.0, 2.35), (5.15, 20.0)], iv
    assert abs((iv[0][1] - 2.0) - 0.35) < 1e-9
    assert abs((5.5 - iv[1][0]) - 0.35) < 1e-9
    assert kept >= 0.75 * 20


def test_overtrim_returns_no_cut():
    # Two long internal gaps. Without the 75% guard the kept audio is ~2.4s of 26.3s.
    words = [word("aaa", 0.1, 0.4), word("bbb", 10.0, 10.3), word("ccc", 26.0, 26.2)]
    iv = keep_intervals(words, 0.0, 26.3)
    print(f"  26.3s window, gaps that would leave ~2.4s -> {iv}")
    assert iv == [(0.0, 26.3)], iv
    # Same shape of gap on a long window still cuts, so this result is the guard, not "never cut".
    long = keep_intervals([word("aaa", 0.1, 0.4), word("bbb", 10.0, 10.3), word("ccc", 11.0, 70.0)], 0.0, 80.0)
    assert len(long) == 2, long


def test_estimated_word_blocks_cut():
    words = [word("aaa", 1.0, 2.0, p=0.5), word("bbb", 5.5, 6.5, p=0.95), word("ccc", 7.0, 16.0)]
    iv = keep_intervals(words, 0.0, 20.0)
    print(f"  3.5s gap bounded by p=0.5 -> {iv}")
    assert iv == [(0.0, 20.0)], iv
    words[0]["p"] = 0.51
    cut = keep_intervals(words, 0.0, 20.0)
    assert len(cut) == 2, cut


def test_autosub_prefers_requested_lang():
    ac = {"ar-orig": [{"ext": "json3"}], "de-orig": [{"ext": "json3"}], "en-orig": [{"ext": "json3"}]}
    got = autosub_key(ac, "en", "ar")
    print(f"  lang=en info_lang=ar keys={list(ac)} -> {got}")
    assert got == "en-orig"
    assert autosub_key(ac, "auto", "en") == "en-orig"
    assert autosub_key(ac, "auto", "") == "ar-orig"
    assert autosub_key({"ar-orig": [{}], "en": [{}]}, "en", "en") == "ar-orig"


def test_clip_rating_values():
    for sc, want in ((-1.8, 4), (1.75, 6), (9, 10)):
        got = clip_rating(sc)
        print(f"  clip_rating({sc}) == {got}")
        assert got == want
    assert clip_rating(-100) == 1
    assert clip_rating(100) == 10


def test_find_moments_stores_rating_and_raw():
    import main
    words = []
    t = 1.0
    for i in range(40):
        words.append(word("hello." if i == 39 else "hello", t, t + 0.3))
        t += 0.6
    real = main.score_window
    main.score_window = lambda *a, **k: (-1.8, ["why"])
    try:
        import numpy as np
        moments = find_moments(words, "en", np.zeros(80), 0.5, None, n=1, dmin=20, dmax=40)
    finally:
        main.score_window = real
    assert moments, "expected one moment"
    m = moments[0]
    print(f"  find_moments score={m['score']} score_raw={m['score_raw']}")
    assert m["score"] == 4 and m["score_raw"] == -1.8


def test_summary_shows_rating_and_raw():
    d = Path(tempfile.mkdtemp())
    clip = {"file": "clip_01.mp4", "start": 1, "end": 20, "duration": 19, "crop_mode": "face", "caption": "c", "hook": "h"}
    summ = {"title": "T", "url": "u", "language": "en", "clips": [
        dict(clip, title="New", score=6, score_raw=1.75),
        dict(clip, title="Old", score=-1.8),
        dict(clip, title="Whole", score=8.0),
    ]}
    write_summary(d, summ)
    md = (d / "summary.md").read_text(encoding="utf-8")
    print("  summary.md score lines:")
    for line in md.splitlines():
        if line.startswith("##"):
            print("   ", line)
    assert "ocena 6/10, raw 1.75" in md
    assert "ocena 4/10, raw -1.8" in md
    assert "(ocena 8/10)" in md


def test_captions_skip_gap_and_allow_24_chars():
    words = [word("keep", 0.2, 0.8), word("gone", 3.0, 3.4), word("stay", 5.2, 5.8)]
    iv = [(0.0, 1.2), (4.8, 6.5)]
    d = Path(tempfile.mkdtemp())
    path = d / "t.ass"
    make_ass(words, iv, path, "Arial", size=40)
    text = path.read_text(encoding="utf-8")
    assert "KEEP" in text and "STAY" in text and "GONE" not in text
    # 10+10 letters plus the joining space is 21, under 24 (the old limit was 18).
    fit = [word("AAAAAAAAAA", 0.1, 0.4), word("BBBBBBBBBB", 0.5, 0.9)]
    p2 = d / "fit.ass"
    make_ass(fit, [(0.0, 2.0)], p2, "Arial", size=40, words_per=8)
    fit_lines = [ln for ln in p2.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue")]
    assert any("AAAAAAAAAA" in ln and "BBBBBBBBBB" in ln for ln in fit_lines), fit_lines
    # 12+12 is 25, still over 24, so they split.
    split = [word("AAAAAAAAAAAA", 0.1, 0.4), word("BBBBBBBBBBBB", 0.5, 0.9)]
    p3 = d / "split.ass"
    make_ass(split, [(0.0, 2.0)], p3, "Arial", size=40, words_per=8)
    split_lines = [ln for ln in p3.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue")]
    assert not any("AAAAAAAAAAAA" in ln and "BBBBBBBBBBBB" in ln for ln in split_lines), split_lines
    print(f"  captions: gap word dropped; 21-char line kept together, 25-char line split ({len(fit_lines)} vs {len(split_lines)} events)")


def test_editor_time_uses_output_clock():
    words = [word("aaa", 1.0, 2.0), word("bbb", 5.5, 6.5), word("ccc", 7.0, 16.0)]
    iv = keep_intervals(words, 0.0, 20.0)
    shown = round(src_to_out(5.5, iv), 2)
    print(f"  editor second for source 5.5s -> {shown} (source offset was {round(5.5 - 0.0, 2)})")
    assert shown == 2.7
    assert round(src_to_out(105.2, [(100.0, 130.0)]), 2) == 5.2  # no cut: same as source minus start
    assert round(src_to_out(4.0, iv), 2) == round(iv[0][1], 2)  # time inside the removed gap sits on the cut


def test_podcasty_label():
    label = finder.PRESETS["podcasty"]["label"]
    print(f"  PRESETS['podcasty']['label'] == {label!r} (en queries kept: {finder.PRESETS['podcasty']['en'][0]!r})")
    assert label == "Podcasty"
    assert finder.PRESETS["podcasty"]["pl"] and finder.PRESETS["podcasty"]["en"]


def _words_from_chunks(chunks, gap=1.0, start=1.0):
    words, t = [], start
    for ch in chunks:
        for w in ch.split():
            words.append(word(w, round(t, 3), round(t + 0.25, 3)))
            t += 0.35
        t += gap
    return words


def _old_title(first_sentence):
    """Previous title: first 9 words of the opening sentence."""
    w = first_sentence.split()
    return " ".join(w[:9]) + ("…" if len(w) > 9 else "")


def test_title_from_sample_words():
    chunks = ["the nd", "with", "their", "on", "One54 on Instagram.",
              "nobody tells you the truth about money and fear."]
    parts = [s["text"] for s in sentences(_words_from_chunks(chunks))]
    old = _old_title(parts[0])
    title = clip_title(parts, "en")
    print(f"  before {old!r}")
    print(f"  after  {title!r}")
    print(f"  sentences: {parts}")
    toks = title.split()
    assert old == "the nd"
    assert len(toks) >= 3 and len(title) >= 15 and len(title) <= 70
    assert title[0].isupper()
    assert toks[0].lower().strip(".,!?…") not in STOP
    assert title.lower() not in STOP and not (len(toks) == 1 and toks[0].lower() in STOP)
    low = title.lower()
    assert "truth" in low or "nobody" in low
    assert "instagram" not in low
    for bad in ("with", "their", "on", "the"):
        assert clip_title([bad], "en") == ""
    filled = clip_title(["Um so the biggest secret nobody tells you about money."], "en")
    print(f"  filler stripped -> {filled!r}")
    assert filled[0].isupper() and filled.split()[0].lower() not in STOP
    assert not filled.lower().startswith("um") and not filled.lower().startswith("the ")
    long = "Nobody tells you the secret truth about money fear and the biggest mistake everyone always makes in life today."
    capped = clip_title([long], "en")
    print(f"  capped ({len(capped)} chars) -> {capped!r}")
    assert 15 <= len(capped) <= 70 and len(capped.split()) >= 3
    assert all(w in long.split() for w in capped.split())
    m = {"hook": parts[0], "text": " ".join(parts), "sents": parts}
    title2, cap, _tags = title_and_tags(m, "en")
    assert title2 == title and "truth" in m["hook"].lower() and "sents" not in m
    assert title2 in cap


def _window_score(text, start, dur):
    import numpy as np
    words = _words_from_chunks([text], gap=0.0, start=start)
    sents = sentences(words)
    loud = np.zeros(int(dur / 0.5) + 20)
    return score_window(sents, 0, len(sents) - 1, words, "en", loud, 0.5, None, dur)


def test_promo_window_scores_lower():
    body = "Nobody tells you the truth about money and the fear that always changes people"
    content, why_c = _window_score(body + " after breakfast today.", 40, 800)
    promo, why_p = _window_score(body + " please subscribe now.", 40, 800)
    pl_base, _ = _window_score("Nikt nie mówi prawdy o pieniądzach i o strachu który zawsze zmienia ludzi po prostu dzisiaj.", 40, 800)
    pl_promo, why_pl = _window_score("Nikt nie mówi prawdy o pieniądzach i o strachu który zawsze zmienia ludzi zostaw lajka teraz.", 40, 800)
    end, why_e = _window_score(body + " please subscribe now.", 760, 800)
    print(f"  content {content:.2f} {why_c}")
    print(f"  promo   {promo:.2f} {why_p}")
    print(f"  pl      {pl_base:.2f} vs promo {pl_promo:.2f} {why_pl}")
    print(f"  outro   {end:.2f} {why_e}")
    assert content > -50 and "promo" not in why_c
    assert promo <= -50 and "promo" in why_p
    assert pl_promo <= -50 and pl_promo < pl_base and "promo" in why_pl
    assert end <= -50 and "promo" in why_e


def _spread(text, start, end):
    """Place words evenly from start to end (a sparse or looped window)."""
    ws = text.split()
    if not ws: return []
    step = (end - start) / len(ws)
    out = []
    for i, w in enumerate(ws):
        s = start + i * step
        out.append(word(w, round(s, 3), round(min(end, s + min(0.3, step * 0.6)), 3)))
    return out


def test_reject_junk_windows_and_titles():
    import re
    import numpy as np
    from types import SimpleNamespace
    # Repeated end-card phrase, a 9-word minute, and a watermark are not clips.
    loop = " ".join(["One54 on Instagram"] * 4)
    sc_loop, why_loop = _window_score(loop, 400, 800)
    sparse_words = _spread("one two three four five six seven eight nine", 325, 385)
    sents = sentences(sparse_words)
    loud = np.zeros(2000)
    sc_sp, why_sp = score_window(sents, 0, len(sents) - 1, sparse_words, "en", loud, 0.5, None, 800)
    url = "📷 www .kondy .pl 🔥🔥🔥"
    sc_url, why_url = _window_score(url, 500, 800)
    print(f"  loop {sc_loop:.1f} {why_loop}")
    print(f"  sparse 9 words / 60s {sc_sp:.1f} {why_sp} n={len(sparse_words)}")
    print(f"  url {sc_url:.1f} {why_url}")
    assert sc_loop <= -50 and sc_sp <= -50 and why_sp == ["sparse"]
    assert len(sparse_words) == 9 and sparse_words[-1]["e"] - sparse_words[0]["s"] > 50
    assert sc_url <= -50
    # Titles: no placeholder, no trailing comma/conjunction, no URL or mixed-script garbage.
    assert clip_title([url], "pl") == ""
    assert clip_title(["with"], "en") == ""
    cut = clip_title(["Miały na przykład nie mieć na sobie metek,"], "pl")
    url_title = clip_title(["Www proszę .youtube .com, państwa przerywamy program aby"], "pl")
    garb = clip_title(["To jest orroviewerенное .ploplesy naprawdę wielki sekret o pieniądzach."], "pl")
    print(f"  cut {cut!r}")
    print(f"  url title {url_title!r}")
    print(f"  garbage {garb!r}")
    for t in (cut, url_title, garb):
        assert t and t.lower() not in {"best spoken moment", "najlepszy moment filmu"}
        assert not re.search(r"[,;]\s*$", t)
        assert t.split()[-1].lower().strip(".,!?…") not in {"aby", "że", "and", "that"}
        assert "www" not in t.lower() and "youtube" not in t.lower() and ".com" not in t.lower()
    assert "metek" in cut.lower()
    assert "aby" not in url_title.lower().split()[-1]
    assert "orroviewer" not in garb.lower() and "ploplesy" not in garb.lower() and "sekret" in garb.lower()
    # Burned captions drop the same garbage tokens.
    caps = [word("Hello", 0.2, 0.6), word("orroviewerенное", 0.7, 1.0), word(".ploplesy", 1.1, 1.4), word("there", 1.5, 1.9)]
    p = Path(tempfile.mkdtemp()) / "c.ass"
    make_ass(caps, [(0.0, 3.0)], p, "Arial", size=40)
    shown = p.read_text(encoding="utf-8")
    assert "HELLO" in shown and "THERE" in shown
    assert "PLOPLESY" not in shown.upper() and "ORROVIEWER" not in shown.upper()
    # Verbatim segment loop and a 3x phrase are removed from the transcript.
    segs = [SimpleNamespace(text="Hello there"), SimpleNamespace(text="One54 on Instagram"),
            SimpleNamespace(text="One54 on Instagram"), SimpleNamespace(text="One54 on Instagram")]
    kept = [s.text for s in _drop_repeat_segs(segs)]
    assert kept == ["Hello there", "One54 on Instagram"]
    phrase = []
    t = 0.0
    for _ in range(4):
        for w in ("One54", "on", "Instagram"):
            phrase.append({"w": w, "s": t, "e": t + 0.2}); t += 0.3
    phrase.append({"w": "Nobody", "s": t, "e": t + 0.2})
    cleaned = _drop_phrase_loops(phrase)
    print(f"  phrase loop kept {[w['w'] for w in cleaned]}")
    assert [w["w"] for w in cleaned] == ["Nobody"]
    # Asked for 5 clips: only the real window comes back, junk is not filler.
    good = _spread(
        "Nobody tells you the secret truth about money fear love and the biggest mistake "
        "everyone always makes when life changes really fast today here friends listen "
        "carefully remember this reason actually matters most right now okay yes",
        10, 34)
    good[-1]["w"] = good[-1]["w"] + "."
    words = good + _spread(" ".join(["One54 on Instagram"] * 6), 100, 140) + sparse_words + _spread(url, 500, 540)
    moments = find_moments(words, "en", np.zeros(1600), 0.5, None, n=5, dmin=20, dmax=60)
    titles = []
    for m in moments:
        title, _, _ = title_and_tags(dict(m), "en")
        titles.append(title)
    print(f"  moments {len(moments)} titles {titles}")
    assert 0 < len(moments) < 5
    assert titles and all(titles)
    blob = " ".join(titles).lower()
    assert "instagram" not in blob and "kondy" not in blob and "best spoken" not in blob
    assert any("truth" in t.lower() or "secret" in t.lower() or "nobody" in t.lower() for t in titles)


def test_presets_language_neutral():
    import re
    assert "podcast_en" not in finder.PRESETS
    assert [k for k in finder.PRESETS if "podcast" in k] == ["podcasty"]
    assert finder.preset_cfg("podcast_en") is finder.PRESETS["podcasty"]
    assert "long form conversation podcast" in finder.PRESETS["podcasty"]["en"]
    assert finder.PRESETS["podcasty"]["pl"][0] == "podcast wywiad"
    assert "gta6" in finder.PRESETS
    g = finder.PRESETS["gta6"]
    assert g["label"] == "GTA 6"
    assert "GTA 6 przecieki" in g["pl"] and "GTA 6 trailer breakdown" in g["en"]
    assert len(g["clip"]) == 2 and g["clip"][0] < g["clip"][1]
    assert "Rockstar" in finder.rights_text("gta6")
    assert finder.rights_text("bajki") == finder.RIGHTS_MSG
    assert finder.rights_text("gta6", "własny temat") == ""
    assert finder.rights_text("podcast_en") == ""
    for k, v in finder.PRESETS.items():
        assert isinstance(v.get("pl"), list) and v["pl"], k
        assert isinstance(v.get("en"), list) and v["en"], k
        assert "lang" not in v, k
        lab = v["label"]
        assert "angielski" not in lab.lower(), lab
        assert not re.search(r"\b(PL|EN)\b", lab), lab
    print(f"  {len(finder.PRESETS)} presets, gta6 clip={g['clip']}, podcast_en -> podcasty")


def test_stale_clip_files_removed():
    d = Path(tempfile.mkdtemp())
    keep = ["summary.json", "summary.md", "notes.txt", "clips_vid.zip"]
    drop = ["clip_01.mp4", "clip_04.mp4", "clip_05.mp4", "clip_05.ass", "clip_05.jpg", "clip_02.srt"]
    for name in keep + drop:
        (d / name).write_bytes(b"old")
    (d / "not_a_clip").mkdir()
    gone = clear_clip_outputs(d)
    left = sorted(p.name for p in d.iterdir())
    print(f"  removed {sorted(gone)}")
    print(f"  left {left}")
    for name in drop:
        assert not (d / name).exists(), name
        assert name in gone
    for name in keep + ["not_a_clip"]:
        assert (d / name).exists(), name
    assert clear_clip_outputs(d) == []


def test_title_keeps_whole_sentence():
    whole = "What good is security if you can see the whole room clearly now."
    print(f"  whole len={len(whole)} -> {clip_title([whole], 'en')!r}")
    assert 60 < len(whole) <= 70
    assert clip_title([whole], "en") == whole
    said = clip_title(["He said why don't you like interview them at all."], "en")
    print(f"  said -> {said!r}")
    assert not said.lower().startswith("he said")
    assert "why" in said.lower() and "interview" in said.lower()
    joined = clip_title(["What good is security", "if you can see the whole room now."], "en")
    print(f"  joined fragments -> {joined!r}")
    assert joined.lower().startswith("what good is security")
    pl = clip_title(["Wyszło nam naprawdę dobrze, że jednak,"], "pl")
    print(f"  trailing conjunctions -> {pl!r}")
    assert not re.search(r"[,;]\s*$", pl)
    last = re.sub(r"[.!?…]+$", "", pl.split()[-1].lower())
    assert last not in {"i", "a", "ale", "że", "jednak", "więc", "bo", "and", "but", "so", "that", "because"}
    assert "jednak" not in [w.lower().strip(".,!?…") for w in pl.split()]
    assert pl.split()[-2].lower().strip(".,!?…") != "że"
    long = ("What good is security if you can see every camera in this room and because "
            "nobody tells you the secret truth about money fear today please listen carefully.")
    cut = clip_title([long], "en")
    end = re.sub(r"[.!?…,;:]+$", "", cut.split()[-1].lower())
    print(f"  cut ({len(cut)}) ends {end!r} -> {cut!r}")
    assert cut.lower().startswith("what good is security")
    assert 15 <= len(cut) <= 70
    assert end not in _TRAIL
    assert "…" not in cut
    assert all(w.strip(".,!?…") in long.replace(".", "").split() or w in long.split() for w in cut.split())


def _burst(text, t, step=0.46):
    words, marks = [], {}
    for raw in text.split():
        if raw == "|":
            t += 0.95
            continue
        if raw == "||":
            t += 0.6
            continue
        words.append(word(raw, round(t, 3), round(t + 0.2, 3)))
        marks.setdefault(raw.strip(".,!?…"), round(t, 3))
        t += step
    return words, t, marks


def test_clip_windows_follow_sentences():
    import numpy as np
    filler = " ".join(f"tok{i}x" for i in range(28))
    s1 = ("What good is | security if you can see the secret truth about money fear love "
          f"orroviewerенное .ploplesy and the biggest mistake everyone always makes {filler} right now.")
    s2 = ("He || Said why don't you like interview them today when the secret truth about money "
          f"and fear always changes people and the biggest mistake matters {filler} please.")
    w1, t, m1 = _burst(s1, 3.0)
    t += 1.5
    w2, t, m2 = _burst(s2, t)
    words = w1 + w2
    print(f"  s1 {w1[0]['s']:.1f}-{w1[-1]['e']:.1f}s ({len(w1)}w) pause-before security@{m1['security']:.1f}")
    print(f"  s2 {w2[0]['s']:.1f}-{w2[-1]['e']:.1f}s ({len(w2)}w) Said@{m2['Said']:.1f}")
    assert w1[-1]["e"] - w1[0]["s"] >= 20
    assert w2[-1]["e"] - w2[0]["s"] >= 20
    moments = find_moments(words, "en", np.zeros(int(t / 0.5) + 30), 0.5, None, n=5, dmin=20, dmax=48)
    described = []
    for m in moments:
        described.append((round(m["start"], 2), round(m["end"], 2), m["text"][:70], clip_title(m["sents"], "en")))
    print("  moments:")
    for row in described:
        print("   ", row)
    assert moments, "expected a clip on each full sentence"
    texts = [m["text"] for m in moments]
    assert any(tx.startswith("What good is") for tx in texts), texts
    assert any(tx.startswith("He Said") or tx.startswith("He said") for tx in texts), texts
    for tx in texts:
        assert not tx.startswith("security"), tx
        assert not tx.lower().startswith("said "), tx
        low = tx.lower()
        assert "orroviewer" not in low and "ploplesy" not in low and "www" not in low
    what = next(m for m in moments if m["text"].startswith("What"))
    he = next(m for m in moments if m["text"].startswith("He"))
    assert what["start"] <= m1["security"] - 0.4
    assert he["start"] <= m2["Said"] - 0.2
    assert clip_title(what["sents"], "en").lower().startswith("what good is security")
    he_title = clip_title(he["sents"], "en").lower()
    assert not he_title.startswith("he said")
    assert "why" in he_title or "interview" in he_title
    ordered = sorted(moments, key=lambda m: m["start"])
    for a, b in zip(ordered, ordered[1:]):
        assert a["end"] <= b["start"] + 0.05, (a["end"], b["start"])


def test_summary_text_drops_garbage():
    raw = "Hello orroviewerенное .ploplesy there www .com friend."
    cleaned = clean_speech(raw)
    print(f"  clean_speech -> {cleaned!r}")
    assert "orroviewer" not in cleaned.lower() and "ploplesy" not in cleaned.lower()
    assert "www" not in cleaned.lower() and "com" not in cleaned.lower().split()
    assert cleaned.startswith("Hello") and "there" in cleaned and "friend" in cleaned
    d = Path(tempfile.mkdtemp())
    clip = {"file": "clip_01.mp4", "start": 1, "end": 20, "duration": 19, "crop_mode": "face",
            "caption": "c", "hook": "Hello orroviewerенное there.", "title": "Hello there",
            "score": 6, "score_raw": 1.0, "text": raw}
    write_summary(d, {"title": "T", "url": "u", "language": "en", "clips": [clip]})
    saved = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    text, hook = saved["clips"][0]["text"], saved["clips"][0]["hook"]
    print(f"  summary text -> {text!r}")
    print(f"  summary hook -> {hook!r}")
    assert "orroviewer" not in text.lower() and "ploplesy" not in text.lower()
    assert "Hello" in text and "there" in text and "friend" in text
    assert "orroviewer" not in hook.lower() and hook.startswith("Hello") and "there" in hook


def _ass_times(path):
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if not ln.startswith("Dialogue"):
            continue
        bits = ln.split(",", 9)
        def parse(ts):
            h, m, s = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        out.append((parse(bits[1]), parse(bits[2]), ln))
    return out


def test_captions_monotonic_and_inside_clip():
    words = [word("second", 1.0, 1.3), word("first", 0.2, 0.5), word("third.", 1.6, 1.9)]
    p = Path(tempfile.mkdtemp()) / "t.ass"
    make_ass(words, [(0.0, 2.0)], p, "Arial", size=40, words_per=1)
    times = _ass_times(p)
    print("  " + " | ".join(f"{a:.2f}-{b:.2f}" for a, b, _ in times))
    assert [a for a, _, _ in times] == sorted(a for a, _, _ in times)
    for a, b, ln in times:
        assert 0 <= a < b <= 2.0 + 1e-6, ln
    blob = p.read_text(encoding="utf-8")
    assert "FIRST" in blob and "SECOND" in blob and "THIRD" in blob
    # same clock the editor shows: a word's caption does not run past the cut clip
    shown = ass_time(src_to_out(1.9, [(0.0, 2.0)]))
    assert shown.startswith("0:00:01.9")


def test_numbers_stay_in_speech():
    kept = ["2008", "2008,", "$10", "10%", "3:45", "1.5", "500", "$200K", "2009", "500zł"]
    for tok in kept:
        assert not _junk_token(tok), tok
    for tok in ("www.kondy.pl", ".ploplesy", "orroviewerенное", "@someone", "https://youtu.be/abc", "www"):
        assert _junk_token(tok), tok
    raw = "$200K back in 2008, what would it have been worth today? Around $10 billion."
    cleaned = clean_speech(raw)
    print(f"  speech -> {cleaned!r}")
    assert "$200K" in cleaned and "2008" in cleaned and "$10" in cleaned
    assert "back in" in cleaned and not cleaned.lower().startswith("back in what")
    title = clip_title([raw], "en")
    print(f"  title -> {title!r}")
    assert "2008" in title or "$200" in title
    assert "10" in title or "billion" in title
    pl = clip_title(["Każde 10 przesyłek idzie dzisiaj osobno."], "pl")
    print(f"  pl -> {pl!r}")
    assert "10" in pl and "przesyłek" in pl
    caps = [word("$200K", 0.2, 0.5), word("back", 0.6, 0.8), word("in", 0.9, 1.1),
            word("2008,", 1.2, 1.6), word(".ploplesy", 1.7, 2.0), word("today", 2.1, 2.4)]
    p = Path(tempfile.mkdtemp()) / "n.ass"
    make_ass(caps, [(0.0, 3.0)], p, "Arial", size=40)
    shown = p.read_text(encoding="utf-8").upper()
    assert "2008" in shown and "200K" in shown and "TODAY" in shown
    assert "PLOPLESY" not in shown


def _story(text, t, step=0.42):
    words = []
    for raw in text.split():
        words.append(word(raw, round(t, 3), round(t + 0.18, 3)))
        t += step
    return words, t


def test_numbers_and_snap_keep_windows():
    import numpy as np
    filler = " ".join(f"tok{i}x" for i in range(32))
    # Three stories, each long enough on its own. Only the last ends with a period,
    # so an aggressive snap would glue the first two together and drop a clip.
    a = (f"What good is security if you can see the secret truth about money fear "
         f"and the biggest mistake everyone always makes {filler} right now")
    b = (f"Back in 2008 and 2009 you put $200K in and around $10 billion is what it "
         f"would be worth today friends listen carefully {filler} please")
    c = (f"Nobody tells you the secret truth about fear love and the biggest mistake "
         f"everyone always makes when life changes {filler} here.")
    w, t = _story(a, 8.0, step=0.5)
    t += 6.0
    w2, t = _story(b, t, step=0.5)
    t += 6.0
    w3, t = _story(c, t, step=0.5)
    words = w + w2 + w3
    print(f"  spans {w[0]['s']:.0f}-{w[-1]['e']:.0f} {w2[0]['s']:.0f}-{w2[-1]['e']:.0f} {w3[0]['s']:.0f}-{w3[-1]['e']:.0f}")
    assert w[-1]["e"] - w[0]["s"] >= 20
    assert w2[-1]["e"] - w2[0]["s"] >= 20
    moments = find_moments(words, "en", np.zeros(int(t / 0.5) + 30), 0.5, None, n=3, dmin=20, dmax=60)
    print(f"  windows {len(moments)}")
    for m in moments:
        print(f"   {m['start']:.1f}-{m['end']:.1f} {m['score_raw']} {m['text'][:80]!r}")
    assert len(moments) == 3, [m["text"][:40] for m in moments]
    blob = " ".join(m["text"] for m in moments)
    assert "What good is security" in blob
    assert "2008" in blob and "2009" in blob and "$200K" in blob and "$10" in blob
    assert "ploplesy" not in blob.lower()
    # snapping a too-short fragment must not delete the in-range window around it
    moments2 = find_moments(words, "en", np.zeros(int(t / 0.5) + 30), 0.5, None, n=1, dmin=20, dmax=60)
    assert len(moments2) == 1


def test_fallback_fills_without_promo():
    import numpy as np
    filler = " ".join(f"word{i}x" for i in range(40))
    strong = f"What good is security if you can see the secret truth about money {filler} today."
    # Starts weak and does not finish the sentence: under MIN_RAW, but still real speech.
    weak = f"and table chair paper morning coffee window garden pencil notebook {filler} quietly"
    promo = "Please subscribe now and follow me on instagram for the secret link in the description today friends listen."
    w, t = _story(strong, 5.0, step=0.48)
    t += 8
    w2, t = _story(weak, t, step=0.48)
    t += 8
    w3, t = _story(promo, t, step=0.5)
    words = w + w2 + w3
    moments = find_moments(words, "en", np.zeros(int(t / 0.5) + 30), 0.5, None, n=3, dmin=20, dmax=50)
    texts = [m["text"] for m in moments]
    print(f"  fallback n={len(moments)}")
    for m in moments:
        print(f"   raw {m['score_raw']} {m['text'][:70]!r}")
    assert any("What good is security" in tx for tx in texts)
    assert any("Table chair" in tx or "table chair" in tx.lower() for tx in texts)
    assert any(m["score_raw"] < MIN_RAW for m in moments)
    blob = " ".join(texts).lower()
    assert "subscribe" not in blob and "instagram" not in blob
    assert len(moments) == 2


def test_allegro_hook_not_backreference():
    """The shipped title was a backwards clause plus grammatical hashtags."""
    bad = "Polegało to na tym, że każde przesyłek odebranych przez Allegro One."
    good = "Allegro One daje drzewka za paczki."
    ctx = "Sadzonki odbierasz razem z zamówieniem."
    before_title = bad.rstrip(".")
    before_tags = "#allegro #każde #przesyłek #sobie #shorts #viral #fyp #polska #dlaciebie"
    m = {
        "text": bad + " " + good + " " + ctx + " weź to sobie",
        "sents": [bad, good, ctx],
        "hook": bad,
    }
    title, cap, tags = title_and_tags(m, "pl", "Allegro One drzewka za paczki")
    print(f"  before title {before_title!r}")
    print(f"  before tags  {before_tags}")
    print(f"  after title  {title!r}")
    print(f"  after tags   {' '.join(tags)}")
    print(f"  after cap    {cap!r}")
    assert title and not title.lower().startswith("polegało")
    assert "każde przesyłek" not in title.lower()
    assert "drzewka" in title.lower() and "allegro" in title.lower()
    low = [t.lower() for t in tags]
    assert "#każde" not in low and "#sobie" not in low and "#przesyłek" not in low
    assert "#allegro" in low
    assert "#fyp" not in low and "#polska" not in low and "#dlaciebie" not in low
    assert len([t for t in low if t in {"#shorts", "#viral", "#fyp", "#polska", "#dlaciebie", "#podcast"}]) <= 2
    assert title in cap and ctx.rstrip(".") in cap
    assert cap.strip() != title
    # only the broken sentence: a topic line, not the clause
    only = clip_title([bad], "pl", "Allegro One drzewka za paczki")
    print(f"  topic-only   {only!r}")
    assert only and not only.lower().startswith("polegało")
    assert "allegro" in only.lower()
    gta = _hashtags_for("GTA 6 przecieki")
    assert "gta6" in gta


def _hashtags_for(video_title):
    from main import _hashtags
    return [t[1:] for t in _hashtags("Nic tu nie ma naprawdę.", "pl", video_title)]


def test_js_rating_matches():
    node = shutil.which("node")
    if not node:
        print("  node missing, skipped JS parity")
        return
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const m = src.match(/const clipRating=sc=>\{[\s\S]*?\};/);
if (!m) throw new Error('clipRating not found');
const clipRating = new Function(m[0] + '\nreturn clipRating;')();
const cases = [[-1.8, 4], [1.75, 6], [8.9, 10], [9, 9], [6, 6], [7.24, 9], [-0.58, 5]];
for (const [sc, want] of cases) {
  const got = clipRating(sc);
  if (got !== want) throw new Error(sc + ' -> ' + got + ' expected ' + want);
  console.log('  js clipRating(' + sc + ') == ' + got);
}
"""
    r = subprocess.run([node, "-e", script, str(ROOT / "app" / "jobs.js")], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.returncode:
        print(r.stderr)
        raise SystemExit(r.returncode)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(fn.__name__)
        fn()
        print("  PASS")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
