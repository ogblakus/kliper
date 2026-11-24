#!/usr/bin/env python3
"""Offline checks for silence cuts, captions, clip rating, titles, and finder presets.

No YouTube and no network. Run: python tests/test_offline.py
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import finder
from main import (
    STOP, autosub_key, clip_rating, clip_title, find_moments, keep_intervals, make_ass,
    score_window, sentences, src_to_out, title_and_tags, write_summary,
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
    assert len(toks) >= 3 and len(title) >= 15 and len(title) <= 60
    assert title[0].isupper()
    assert toks[0].lower().strip(".,!?…") not in STOP
    assert title.lower() not in STOP and not (len(toks) == 1 and toks[0].lower() in STOP)
    low = title.lower()
    assert "truth" in low or "nobody" in low
    assert "instagram" not in low
    for bad in ("with", "their", "on", "the"):
        got = clip_title([bad], "en")
        assert got.lower() != bad and len(got.split()) >= 3 and got.lower() not in STOP
    filled = clip_title(["Um so the biggest secret nobody tells you about money."], "en")
    print(f"  filler stripped -> {filled!r}")
    assert filled[0].isupper() and filled.split()[0].lower() not in STOP
    assert not filled.lower().startswith("um") and not filled.lower().startswith("the ")
    long = "Nobody tells you the secret truth about money fear and the biggest mistake everyone always makes in life today."
    capped = clip_title([long], "en")
    print(f"  capped ({len(capped)} chars) -> {capped!r}")
    assert 15 <= len(capped) <= 60 and len(capped.split()) >= 3
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
    assert promo < content and "promo" in why_p and "promo" not in why_c
    assert pl_promo < pl_base and "promo" in why_pl
    assert end < promo and "outro" in why_e and "outro" not in why_p


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
