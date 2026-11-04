"""Wykrywanie sprzętu i dobór ustawień Whispera (preset Szybki / Zbalansowany / Dokładny)."""
import os, shutil, subprocess, functools

def _ram_gb():
    try:
        if os.name == "nt":
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = M(); m.l = ctypes.sizeof(M); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)); return m.total / 2**30
        with open("/proc/meminfo") as f: return int(f.readline().split()[1]) / 2**20
    except Exception: return 8.0

@functools.lru_cache(1)
def detect():
    cores = os.cpu_count() or 4; gpu = ""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8,
                               **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            if r.returncode == 0 and r.stdout.strip(): n, mem = r.stdout.strip().splitlines()[0].rsplit(",", 1); gpu = f"{n.strip()} ({int(float(mem))//1024} GB)"
        except Exception: pass
    ram = round(_ram_gb(), 1)
    tier = "gpu" if gpu else ("strong" if cores >= 12 and ram >= 15 else ("mid" if cores >= 8 and ram >= 7.5 else "weak"))
    return {"cores": cores, "ram_gb": ram, "gpu": gpu, "tier": tier}

# model, beam per tier and preset
TABLE = {
    "gpu":    {"szybki": ("small", 1), "zbalansowany": ("medium", 2), "dokladny": ("large-v3", 5)},
    "strong": {"szybki": ("base", 1),  "zbalansowany": ("small", 2),  "dokladny": ("medium", 5)},
    "mid":    {"szybki": ("base", 1),  "zbalansowany": ("small", 1),  "dokladny": ("medium", 3)},
    "weak":   {"szybki": ("base", 1),  "zbalansowany": ("base", 2),   "dokladny": ("small", 5)},
}
# seconds of processing per 1 min of audio on 8 CPU threads, int8 (measured on test box) – scaled by cores; GPU ~10x faster
SPM = {"tiny": 1.2, "base": 2.5, "small": 6.5, "medium": 30.0, "large-v3": 60.0}

def pick(preset="zbalansowany", model="auto", device="auto"):
    h = detect(); tier = h["tier"] if device != "cpu" or h["tier"] != "gpu" else ("strong" if h["cores"] >= 12 else "mid")
    m, beam = TABLE[tier].get(preset, TABLE[tier]["zbalansowany"])
    if model and model != "auto": m = model
    return {"model": m, "beam": beam, "tier": tier, "hw": h, "preset": preset}

def estimate_min(video_min, model, tier, cores, hybrid=True):
    """Rough total minutes for a video (download+transcription+5 clips)."""
    spm = SPM.get(model, 8) * (8 / max(cores, 2)) ** 0.8
    if tier == "gpu": spm /= 10
    audio = min(video_min, 8) if hybrid else video_min   # hybrid: YouTube subtitles + re-transcribe only ~7–8 min of candidates
    render = 5 * (14 if cores < 8 else 7)                # seconds per clip (2 in parallel on 8+ threads)
    return max(1, round((audio * spm * 1.15 + render + 40) / 60))  # +model load, spellcheck, download
