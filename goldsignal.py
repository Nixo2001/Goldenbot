#!/usr/bin/env python3
import json, os, pathlib, sys, traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd, requests, yfinance as yf

BASE = pathlib.Path(__file__).parent

for _f in ("config.json", "contesto.py", "requirements.txt"):
    if not (BASE / _f).exists():
        print(f"FILE MANCANTE: {_f}")
        sys.exit(1)

_raw = (BASE / "config.json").read_text(encoding="utf-8")
for _a, _b in [("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'")]:
    _raw = _raw.replace(_a, _b)
CFG = json.loads(_raw)

ST = BASE / "stato.json"
TZ = ZoneInfo("Europe/Rome")
TOKEN = os.environ.get("TELEGRAM_TOKEN") or CFG.get("telegram_token", "")
API = f"https://api.telegram.org/bot{TOKEN}"

try:
    import contesto
except Exception:
    contesto = None

def stato():
    if ST.exists():
        return json.loads(ST.read_text(encoding="utf-8"))
    return {"chat_id": CFG.get("chat_id", ""), "running": True, "signals": []}

def salva(s):
    ST.write_text(json.dumps(s, indent=1), encoding="utf-8")

def tg(s, testo):
    if not s.get("chat_id"):
        return
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": s["chat_id"], "text": testo}, timeout=10)
    except Exception:
        pass

def oggi(s):
    d = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for x in s["signals"] if x["ts"][:10] == d)

def perdite(s):
    d = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for x in s["signals"] if x["ts"][:10] == d and x.get("esito") == "LOSS")

def comandi(s):
    try:
        r = requests.get(f"{API}/getUpdates", params={"timeout": 0}, timeout=15).json()
    except Exception:
        return
    for u in r.get("result", []):
        m = u.get("message") or {}
        cid = str(m.get("chat", {}).get("id", ""))
        t = (m.get("text") or "").strip().lower()
        if not t:
            continue
        if not s.get("chat_id"):
            s["chat_id"] = cid; salva(s)
            tg(s, "Bot attivato. Scrivi /status."); continue
        if cid != s["chat_id"]:
            continue
        if t == "/start":
            s["running"] = True; tg(s, "Motore ATTIVO.")
        elif t == "/stop":
            s["running"] = False; tg(s, "Motore in PAUSA.")
        elif t == "/status":
            tg(s, f"Stato: {'ATTIVO' if s['running'] else 'PAUSA'}\n"
                  f"Modalita: {CFG['modalita']}\nOggi: {oggi(s)} segnali, {perdite(s)} stop.\n"
                  f"Payout minimo: {CFG['payout_min']}%")
        elif t.startswith("/payout"):
            try:
                CFG["payout_min"] = int(t.split()[1])
                (BASE / "config.json").write_text(json.dumps(CFG, indent=2), encoding="utf-8")
                tg(s, f"Payout minimo aggiornato a {CFG['payout_min']}%.")
            except Exception:
                tg(s, "Uso: /payout 80")
        elif t == "/stats":
            ch = [x for x in s["signals"] if x.get("esito") in ("WIN", "LOSS")]
            if not ch:
                tg(s, "Nessuna operazione chiusa.")
            else:
                w = sum(1 for x in ch if x["esito"] == "WIN")
                tg(s, f"Chiuse: {len(ch)}\nVinte: {w}\nPerse: {len(ch)-w}\n"
                      f"Win rate: {100*w/len(ch):.1f}%\n"
                      f"Soglia di sopravvivenza: {100/(1+CFG['payout_min']/100):.1f}%")
        elif t == "/report":
            d = datetime.now(timezone.utc).date().isoformat()
            righe = [x for x in s["signals"] if x["ts"][:10] == d]
            tg(s, "REPORT ODIERNO\n" + ("\n".join(
                f"{x['sym']} {x['dir']} -> {x.get('esito') or 'aperto'}" for x in righe)
                or "Nessun segnale."))

def fetch(sym, interval, period):
    df = yf.download(sym, interval=interval, period=period, progress=False,
                     auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()

def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def prezzo_a(sym, quando):
    df = fetch(sym, "1m", "2d")
    if df.empty:
        return None
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    diff = abs((idx - quando).total_seconds())
    if diff.min() > 240:
        return None
    return float(df["Close"].iloc[int(diff.argmin())])

def blackout():
    if CFG["modalita"] == "otc":
        return False
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10).json()
        n = datetime.now(timezone.utc)
        for e in r:
            if e.get("impact") != "High":
                continue
            d = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if abs((d - n).total_seconds()) < 1800:
                return True
    except Exception:
        pass
    return False

def in_finestra():
    if CFG["modalita"] == "otc":
        return True
    o = datetime.now(TZ).strftime("%H:%M")
    return any(a <= o <= b for a, b in CFG["finestre"])

def analizza(sym):
    d5, d1h = fetch(sym, "5m", "5d"), fetch(sym, "1h", "1mo")
    if len(d5) < 120 or len(d1h) < 220:
        return None
    a = atr(d5).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    perc = (atr(d5).iloc[-200:] < a).mean() * 100
    if perc < 35 or perc > 90:
        return None
    e50 = d1h["Close"].ewm(span=50).mean().iloc[-1]
    e200 = d1h["Close"].ewm(span=200).mean().iloc[-1]
    c1 = d1h["Close"].iloc[-1]
    if e50 > e200 and c1 > e50:
        bias = "CALL"
    elif e50 < e200 and c1 < e50:
        bias = "PUT"
    else:
        return None

    u, pr = d5.iloc[-1], d5.iloc[-2]
    if abs(u["Close"] - u["Open"]) < 0.5 * a:
        return None
    if bias == "CALL" and not (u["Close"] > u["Open"] and u["Close"] > pr["Close"]):
        return None
    if bias == "PUT" and not (u["Close"] < u["Open"] and u["Close"] < pr["Close"]):
        return None

    sc, det = 60, ["trend H1", "momentum M5"]
    if 45 <= perc <= 75:
        sc += 15; det.append("volatilita ideale")
    if contesto:
        try:
            ct = contesto.raccogli({"livello2": CFG.get("livello2", {})})
            ds, mult, veto, note = contesto.valuta(ct, "LONG" if bias == "CALL" else "SHORT")
            if veto:
                return None
            sc += ds; det += note
        except Exception:
            pass
    if sc < CFG["score_minimo"]:
        return None

    exp = CFG["expiry_otc_min"] if CFG["modalita"] == "otc" else CFG["expiry_reale_min"]
    return dict(dir=bias, entry=float(u["Close"]), expiry=exp, score=int(sc), det=det, a=float(a))

def monitor(s):
    ora = datetime.now(timezone.utc)
    for x in [y for y in s["signals"] if y["stato"] == "ACTIVE"]:
        scad = datetime.fromisoformat(x["ts"]) + timedelta(minutes=x["expiry"])
        if ora < scad:
            continue
        p = prezzo_a(x["sym"], scad)
        if p is None:
            continue
        vinto = (x["dir"] == "CALL" and p > x["entry"]) or (x["dir"] == "PUT" and p < x["entry"])
        x["stato"], x["esito"] = "CLOSED", "WIN" if vinto else "LOSS"
        tg(s, f"ESITO #{x['id']} {x['sym']} {x['dir']}: "
              f"{'VINTO' if vinto else 'PERSO'} ({x['entry']:.5f} -> {p:.5f})")

def scansione(s):
    if not s["running"] or not in_finestra() or blackout():
        return
    if oggi(s) >= CFG["max_segnali_giorno"] or perdite(s) >= CFG["stop_dopo_perdite"]:
        return
    if len([x for x in s["signals"] if x["stato"] == "ACTIVE"]) >= 1:
        return
    for sym in CFG["attivi"]:
        r = analizza(sym)
        if not r:
            continue
        nid = (max([x["id"] for x in s["signals"]]) + 1) if s["signals"] else 1
        s["signals"].append(dict(id=nid, ts=datetime.now(timezone.utc).isoformat(), dir=r["dir"],
            entry=r["entry"], score=r["score"], stato="ACTIVE", esito=None,
            nota=", ".join(r["det"]), sym=sym, expiry=r["expiry"]))
        tg(s, f"BINARIO {sym.replace('=X','')} — {r['dir']}\n"
              f"Prezzo: {r['entry']:.5f}\n"
              f"Scadenza: {r['expiry']} min\n"
              f"Payout minimo: {CFG['payout_min']}% (VERIFICA A SCHERMO)\n"
              f"Puntata: {CFG['risk_pct']}% fisso\n"
              f"Score: {r['score']}\nMotivo: {', '.join(r['det'])}")
        break

if __name__ == "__main__":
    s = stato()
    comandi(s)
    try:
        monitor(s)
        scansione(s)
    except Exception:
        traceback.print_exc()
    salva(s)
