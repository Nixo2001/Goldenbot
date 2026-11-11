import io, json, pathlib, time
from datetime import datetime
import pandas as pd, requests

BASE = pathlib.Path(__file__).parent
CACHE = BASE / "cache"
CACHE.mkdir(exist_ok=True)

ESC = ["war", "missile", "sanction", "invasion", "coup", "attack", "crisis",
       "strike", "escalat", "conflict", "mobiliz"]
DEESC = ["ceasefire", "truce", "talks", "deal", "agreement", "pause", "ease", "resume"]

def _cache(nome, ttl_min, scarica):
    f = CACHE / nome
    if f.exists() and time.time() - f.stat().st_mtime < ttl_min * 60:
        return f.read_bytes()
    try:
        b = scarica()
        f.write_bytes(b)
        return b
    except Exception:
        return f.read_bytes() if f.exists() else b""

def gpr():
    def dl():
        r = requests.get("https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls", timeout=30)
        r.raise_for_status(); return r.content
    b = _cache("gpr.xls", 1440, dl)
    if not b: return 50.0
    try:
        df = pd.read_excel(io.BytesIO(b))
        col = "GPRD" if "GPRD" in df.columns else [c for c in df.columns if "GPR" in str(c)][0]
        s = pd.to_numeric(df[col], errors="coerce").dropna().tail(240)
        return float((s < s.iloc[-1]).mean() * 100)
    except Exception:
        return 50.0

def gdelt():
    q = '(gold OR bullion OR "central bank") (war OR missile OR sanctions OR invasion OR coup OR attack OR crisis OR ceasefire OR truce OR talks)'
    def dl():
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query": q, "mode": "artlist", "maxrecords": 75,
                    "timespan": "12h", "format": "json", "sourcelang": "english"}, timeout=20)
        r.raise_for_status(); return r.content
    b = _cache("gdelt.json", 10, dl)
    if not b: return 50.0
    try:
        arts = json.loads(b).get("articles", [])
    except Exception:
        return 50.0
    if not arts: return 50.0
    e = d = 0
    for a in arts:
        t = (a.get("title") or "").lower()
        e += sum(t.count(w) for w in ESC)
        d += sum(t.count(w) for w in DEESC)
    if e + d == 0: return 50.0
    return float(min(100, max(0, 50 + 50 * (e - d) / (e + d))))

def cot():
    def dl():
        for y in (datetime.now().year, datetime.now().year - 1):
            r = requests.get(f"https://www.cftc.gov/files/dea/history/dea_fut_xls_{y}.zip", timeout=40)
            if r.ok: return r.content
        raise RuntimeError
    b = _cache("cot.zip", 10080, dl)
    if not b: return 50.0
    try:
        df = pd.read_excel(io.BytesIO(b))
        sel = df[df["Market_and_Exchange_Names"].str.contains("GOLD", case=False, na=False)]
        net = (sel["NonComm_Positions_Long_All"] - sel["NonComm_Positions_Short_All"]).astype(float)
        return float((net.tail(156) < net.iloc[-1]).mean() * 100)
    except Exception:
        return 50.0

def gld():
    urls = ["https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv",
            "https://www.spdrgoldshares.com/media/GLD/file/GLD_US_archive_EN.csv"]
    def dl():
        for u in urls:
            r = requests.get(u, timeout=30)
            if r.ok and len(r.content) > 500: return r.content
        raise RuntimeError
    b = _cache("gld.csv", 360, dl)
    if not b: return 0.0
    try:
        df = pd.read_csv(io.BytesIO(b))
        col = [c for c in df.columns if "tonne" in c.lower()][0]
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        return float(s.iloc[-1] - s.iloc[-6])
    except Exception:
        return 0.0

def raccogli(cfg):
    c = cfg.get("livello2", {})
    out = {"gpr": None, "gdelt": None, "cot": None, "gld": None}
    if c.get("gpr"): out["gpr"] = gpr()
    if c.get("gdelt"): out["gdelt"] = gdelt()
    if c.get("cot"): out["cot"] = cot()
    if c.get("gld"): out["gld"] = gld()
    return out

def valuta(ct, bias):
    d, mult, note = 0, 1.0, []
    g, n, c, l = ct.get("gpr"), ct.get("gdelt"), ct.get("cot"), ct.get("gld")
    if n is not None and n >= 85:
        return 0, 1.0, True, ["veto: shock geopolitico"]
    if g is not None:
        if g >= 95:
            d += 10 if bias == "LONG" else -5; mult = 0.5; note.append(f"GPR {g:.0f} estremo")
        elif g <= 20:
            d += 2 if bias == "LONG" else 0; note.append("GPR basso")
    if n is not None:
        if n >= 65: d += 8 if bias == "LONG" else -6; note.append(f"GDELT {n:.0f} teso")
        elif n <= 35: d += 6 if bias == "SHORT" else -4; note.append(f"GDELT {n:.0f} calmo")
    if c is not None:
        if c >= 90: d += -8 if bias == "LONG" else 6; note.append(f"COT {c:.0f} affollato")
        elif c <= 10: d += 6 if bias == "LONG" else -6; note.append(f"COT {c:.0f} scarico")
    if l is not None:
        if l <= -5: d += -6 if bias == "LONG" else 0; note.append(f"GLD {l:+.1f}t uscita")
        elif l >= 5: d += 5 if bias == "LONG" else 0; note.append(f"GLD {l:+.1f}t entrata")
    return max(-20, min(20, d)), mult, False, note
