"""
fog_verification.py — v2
========================

Verifikation der Briefing-Vorhersagen gegen eingetretene METAR-Beobachtungen.

Zwei Datenquellen:
  A) Direkte +24h-Nebelvorhersage (CAPS-2m-Spread, eine GRIB-Datei je Lauf)
  B) Archivierte Briefings (briefings/briefing_*.txt): Block-0-Ampeln der
     Hauptplaetze fuer ALLE Vorlaufzeiten 6..48 h. Einmal geerntete Werte
     bleiben dauerhaft in der Historie, auch wenn das Archiv rotiert.

Beobachtbare Parameter (METAR):
  FOG: NOGO bei Sicht < 1.6 km oder FG/FZFG; WARN bei < 5 km oder BR
  XW:  Boeen- (sonst Wind-)Komponente quer zur Piste, Schwellen 10/20 kt
       (METAR-Windrichtungen sind rechtweisend -> direkt vergleichbar)
  CIG: niedrigste BKN/OVC/VV-Schicht in ft, Schwellen 5000/2000
HW (8000 ft) und ICE sind vom Boden nicht messbar und werden nicht bewertet.

Ausgaben:
  verification/history.json               Historie (append-only)
  verification/fog_verification.png       Nebel-Zeitreihen je Hauptplatz
  verification/briefing_verification.png  Trefferquote je Platz/Parameter
                                          und nach Vorlaufzeit

Abhaengigkeiten: pip install httpx xarray cfgrib numpy matplotlib
Erwartet caps_route_forecast.py im selben Verzeichnis (RUNWAY_TRUE).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr

from caps_route_forecast import RUNWAY_TRUE

CAPS_BASE = "https://dd.weather.gc.ca/today/model_caps/3km"
AWC_API = "https://aviationweather.gov/api/data"
HISTORY = Path("verification/history.json")
PLOT_FOG = Path("verification/fog_verification.png")
PLOT_BRIEF = Path("verification/briefing_verification.png")
BRIEFING_DIR = Path("briefings")

MAIN_AIRPORTS = [
    ("CYFB", "Iqaluit",       63.756,  -68.556),
    ("CYIO", "Pond Inlet",    72.683,  -77.967),
    ("CYRB", "Resolute Bay",  74.717,  -94.969),
    ("CYHK", "Gjoa Haven",    68.636,  -95.850),
    ("CYCB", "Cambridge Bay", 69.108, -105.138),
    ("PABR", "Utqiagvik",     71.285, -156.766),
    ("PAOM", "Nome",          64.512, -165.445),
]
MAIN_ICAO = {a[0] for a in MAIN_AIRPORTS}

CAT_NUM = {"OK": 0, "WARN": 1, "NOGO": 2}
CAT_COLOR = {"OK": "#2e8b57", "WARN": "#e69500", "NOGO": "#c62828"}
PARAMS = ["FOG", "XW", "CIG"]
LEAD_BUCKETS = [(0, 12, "0-12h"), (13, 24, "13-24h"), (25, 48, "25-48h")]
MATCH_TOLERANCE_H = 3.0
MS_TO_KT = 1.9438  # unused, METAR liefert kt direkt


# ---------------------------------------------------------------------------
# Kategorisierung
# ---------------------------------------------------------------------------

def cat_from_spread(spread_c: float) -> str:
    return "NOGO" if spread_c <= 1.5 else ("WARN" if spread_c <= 3.0 else "OK")


def cat_xw(kt: float) -> str:
    return "OK" if kt <= 10 else ("WARN" if kt <= 20 else "NOGO")


def cat_cig(ft: float) -> str:
    return "OK" if ft >= 5000 else ("WARN" if ft >= 2000 else "NOGO")


def parse_metar(raw: str) -> dict:
    """Sicht, Nebelkategorie, Wind, Boeen, Ceiling aus rohem METAR."""
    vis_km = None
    m = re.search(r"\b(?:M)?(\d+)?(?:\s+)?(\d)/(\d)SM\b", raw)
    if m:
        whole = int(m.group(1)) if m.group(1) else 0
        vis_km = (whole + int(m.group(2)) / int(m.group(3))) * 1.609
    else:
        m = re.search(r"\b(\d+)SM\b", raw)
        if m:
            vis_km = int(m.group(1)) * 1.609
    fog = re.search(r"\b(?:\+|-)?(?:FZ)?FG\b", raw) is not None
    mist = re.search(r"\bBR\b", raw) is not None
    if fog or (vis_km is not None and vis_km < 1.6):
        fog_cat = "NOGO"
    elif mist or (vis_km is not None and vis_km < 5.0):
        fog_cat = "WARN"
    else:
        fog_cat = "OK"

    wind_dir = wind_kt = gust_kt = None
    m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b", raw)
    if m:
        wind_dir = None if m.group(1) == "VRB" else int(m.group(1))
        wind_kt = int(m.group(2))
        gust_kt = int(m.group(3)) if m.group(3) else None

    ceiling_ft = None
    layers = [int(h) * 100 for _t, h in
              re.findall(r"\b(VV|BKN|OVC)(\d{3})\b", raw)]
    if layers:
        ceiling_ft = min(layers)

    return {"vis_km": vis_km, "cat": fog_cat, "wind_dir": wind_dir,
            "wind_kt": wind_kt, "gust_kt": gust_kt, "ceiling_ft": ceiling_ft}


def obs_xw_cat(o: dict, icao: str) -> str | None:
    """Beobachtete Crosswind-Kategorie aus METAR-Wind gegen Pistenrichtung."""
    spd = o.get("gust_kt") or o.get("wind_kt")
    if spd is None:
        return None
    rwy = RUNWAY_TRUE.get(icao)
    if o.get("wind_dir") is None or rwy is None:   # VRB o. Piste unbekannt:
        return cat_xw(spd)                          # volle Boe, konservativ
    xw = spd * abs(math.sin(math.radians(o["wind_dir"] - rwy)))
    return cat_xw(xw)


def obs_cig_cat(o: dict) -> str:
    ft = o.get("ceiling_ft")
    return "OK" if ft is None else cat_cig(ft)      # keine Schicht = frei


# ---------------------------------------------------------------------------
# Beobachtungen und +24h-Direktvorhersage (wie v1, erweitert)
# ---------------------------------------------------------------------------

async def fetch_observations(client: httpx.AsyncClient, now: datetime) -> list[dict]:
    ids = ",".join(a[0] for a in MAIN_AIRPORTS)
    try:
        r = await client.get(f"{AWC_API}/metar",
                             params={"ids": ids, "format": "raw"},
                             headers={"User-Agent": "fog-verification/2.0"},
                             timeout=45.0)
        raw = r.text if r.status_code == 200 else ""
    except httpx.HTTPError:
        raw = ""
    obs = []
    for line in raw.splitlines():
        line = line.strip()
        for icao in MAIN_ICAO:
            if line.startswith((icao, f"METAR {icao}", f"SPECI {icao}")):
                d = parse_metar(line)
                obs.append({"time": now.isoformat(), "icao": icao,
                            **d, "raw": line[:120]})
                break
    return obs


def latest_run(now: datetime) -> tuple[str, str, datetime]:
    c = now - timedelta(hours=6)
    run = "12" if c.hour >= 12 else "00"
    run_dt = c.replace(hour=int(run), minute=0, second=0, microsecond=0)
    return c.strftime("%Y%m%d"), run, run_dt


async def fetch_forecasts(client: httpx.AsyncClient, now: datetime) -> list[dict]:
    """CAPS-2m-Spread fuer valid = jetzt+24h (eine GRIB-Datei)."""
    valid = now + timedelta(hours=24)
    date, run, run_dt = latest_run(now)
    fh = round((valid - run_dt).total_seconds() / 3600)
    for _attempt in range(2):
        if not 1 <= fh <= 48:
            return []
        url = (f"{CAPS_BASE}/{run}/{fh:03d}/{date}T{run}Z_MSC_CAPS_"
               f"DewPointDepression_AGL-2m_RLatLon0.03_PT{fh:03d}H.grib2")
        try:
            r = await client.get(url, timeout=90.0)
        except httpx.HTTPError as exc:
            print(f"CAPS-Abruf: {exc}", file=sys.stderr)
            return []
        if r.status_code == 200:
            break
        fb = run_dt - timedelta(hours=12)
        date, run, run_dt = fb.strftime("%Y%m%d"), f"{fb.hour:02d}", fb
        fh += 12
    else:
        return []

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(r.content)
        gpath = Path(tf.name)
    try:
        ds = xr.open_dataset(gpath, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        lat2d = ds["latitude"].values
        lon2d = np.where(ds["longitude"].values > 180,
                         ds["longitude"].values - 360, ds["longitude"].values)
        field = ds[next(iter(ds.data_vars))].values.squeeze()
        vt = run_dt + timedelta(hours=fh)
        out = []
        for icao, _n, lat, lon in MAIN_AIRPORTS:
            d2 = ((lat2d - lat) ** 2
                  + ((lon2d - lon) * math.cos(math.radians(lat))) ** 2)
            j, i = np.unravel_index(np.argmin(d2), d2.shape)
            sp = float(field[j, i])
            out.append({"made": now.isoformat(), "valid": vt.isoformat(),
                        "icao": icao, "spread": round(sp, 2),
                        "cat": cat_from_spread(sp)})
        ds.close()
        return out
    finally:
        gpath.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Briefing-Ernte: Block-0-Tabellen aus archivierten Briefings
# ---------------------------------------------------------------------------

HDR_RE = re.compile(r"^--- ([A-Z]{4}) ")
ROW_RE = re.compile(r"^\s*(\d{2})\. (\d{2})Z\s+(.+?)\s+\[(OK|WARN|NOGO)\]\s*$")
TOKEN_RE = re.compile(
    r"(>5000|[+\-]?\d+(?:\.\d+)?|—|\?)(~?)(?:\s+(OK|WARN|NOGO))?")
STAMP_RE = re.compile(r"briefing_(\d{8})T(\d{4})Z\.txt$")


def resolve_valid(made: datetime, day: int, hour: int) -> datetime | None:
    """Tag/Stunde einer Tabellenzeile in volle Zeit aufloesen (Monatswechsel)."""
    for k in (0, 1):
        y = made.year + (made.month - 1 + k) // 12
        mo = (made.month - 1 + k) % 12 + 1
        try:
            cand = made.replace(year=y, month=mo, day=day, hour=hour,
                                minute=0, second=0, microsecond=0)
        except ValueError:
            continue
        if -6 * 3600 <= (cand - made).total_seconds() <= 60 * 3600:
            return cand
    return None


def _num(tok: str) -> float | None:
    if tok == ">5000":
        return 99999.0
    try:
        return float(tok)
    except ValueError:
        return None


def parse_briefing(text: str, made: datetime, source: str) -> list[dict]:
    rows, icao = [], None
    for line in text.splitlines():
        if line.startswith("BLOCK 1"):
            break
        h = HDR_RE.match(line.strip())
        if h:
            icao = h.group(1) if h.group(1) in MAIN_ICAO else None
            continue
        if icao is None:
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        toks = TOKEN_RE.findall(m.group(3))
        if len(toks) != 6:                 # HW XW CIG SP TRD ICE erwartet
            continue
        valid = resolve_valid(made, int(m.group(1)), int(m.group(2)))
        if valid is None:
            continue
        hw, xw, cig, sp, trd, ice = toks
        rows.append({
            "source": source, "made": made.isoformat(),
            "valid": valid.isoformat(), "icao": icao,
            "lead_h": round((valid - made).total_seconds() / 3600),
            "xw": _num(xw[0]), "xw_cat": xw[2] or None,
            "cig": _num(cig[0]), "cig_cat": cig[2] or None,
            "sp": _num(sp[0]), "fog_cat": sp[2] or None,
            "total": m.group(4)})
    return rows


def harvest_briefings(history: dict) -> int:
    ingested = set(history.setdefault("ingested_briefings", []))
    new = 0
    if not BRIEFING_DIR.exists():
        return 0
    for path in sorted(BRIEFING_DIR.glob("briefing_*.txt")):
        if path.name in ingested:
            continue
        m = STAMP_RE.search(path.name)
        if not m:
            continue
        made = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M") \
            .replace(tzinfo=timezone.utc)
        rows = parse_briefing(path.read_text(encoding="utf-8",
                                             errors="replace"), made, path.name)
        history.setdefault("briefing_forecasts", []).extend(rows)
        history["ingested_briefings"].append(path.name)
        new += len(rows)
    return new


# ---------------------------------------------------------------------------
# Paarung und Kennzahlen
# ---------------------------------------------------------------------------

def _nearest_obs(obs_by_icao, icao, valid):
    best, best_dt = None, MATCH_TOLERANCE_H
    for o in obs_by_icao.get(icao, []):
        dt = abs((datetime.fromisoformat(o["time"]) - valid)
                 .total_seconds()) / 3600
        if dt < best_dt:
            best, best_dt = o, dt
    return best


def _obs_index(history):
    idx = {}
    for o in history.get("observations", []):
        idx.setdefault(o["icao"], []).append(o)
    return idx


def match_fog_pairs(history: dict, now: datetime) -> list[dict]:
    """Direkte +24h-Nebelvorhersagen (Quelle A) paaren — wie v1."""
    idx = _obs_index(history)
    pairs = []
    for f in history.get("forecasts", []):
        valid = datetime.fromisoformat(f["valid"])
        if valid > now:
            continue
        o = _nearest_obs(idx, f["icao"], valid)
        if o:
            pairs.append({"icao": f["icao"], "valid": f["valid"],
                          "fcat": f["cat"], "ocat": o["cat"],
                          "spread": f["spread"], "vis_km": o.get("vis_km")})
    return pairs


def match_briefing_pairs(history: dict, now: datetime) -> list[dict]:
    """Briefing-Ampeln (Quelle B) je Parameter paaren."""
    idx = _obs_index(history)
    pairs = []
    for f in history.get("briefing_forecasts", []):
        valid = datetime.fromisoformat(f["valid"])
        if valid > now:
            continue
        o = _nearest_obs(idx, f["icao"], valid)
        if not o:
            continue
        combos = [("FOG", f.get("fog_cat"), o["cat"]),
                  ("XW", f.get("xw_cat"), obs_xw_cat(o, f["icao"])),
                  ("CIG", f.get("cig_cat"), obs_cig_cat(o))]
        for param, fcat, ocat in combos:
            if fcat and ocat:
                pairs.append({"icao": f["icao"], "param": param,
                              "valid": f["valid"], "lead_h": f["lead_h"],
                              "fcat": fcat, "ocat": ocat})
    return pairs


def skill(pairs: list[dict]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "acc": None, "pod": None, "far": None}
    acc = sum(p["fcat"] == p["ocat"] for p in pairs) / n
    obs_ev = [p for p in pairs if p["ocat"] != "OK"]
    fc_ev = [p for p in pairs if p["fcat"] != "OK"]
    pod = (sum(p["fcat"] != "OK" for p in obs_ev) / len(obs_ev)
           if obs_ev else None)
    far = (sum(p["ocat"] == "OK" for p in fc_ev) / len(fc_ev)
           if fc_ev else None)
    return {"n": n, "acc": acc, "pod": pod, "far": far}


# ---------------------------------------------------------------------------
# Grafiken
# ---------------------------------------------------------------------------

def render_fog_plot(pairs: list[dict], now: datetime) -> None:
    fig, axes = plt.subplots(len(MAIN_AIRPORTS), 1, sharex=True,
                             figsize=(11, 2.0 * len(MAIN_AIRPORTS)))
    fig.suptitle("CAPS-Nebelvorhersage (+24 h) vs. METAR — "
                 f"Stand {now:%Y-%m-%d %H:%M} UTC\n"
                 "Linie = Vorhersage, Punkte = Beobachtung", fontsize=11)
    for ax, (icao, name, *_c) in zip(np.atleast_1d(axes), MAIN_AIRPORTS):
        p = sorted((q for q in pairs if q["icao"] == icao),
                   key=lambda q: q["valid"])
        s = skill(p)
        title = f"{icao} {name}"
        if s["n"]:
            title += (f"   n={s['n']}  ACC={s['acc']:.0%}"
                      + (f"  POD={s['pod']:.0%}" if s["pod"] is not None else "")
                      + (f"  FAR={s['far']:.0%}" if s["far"] is not None else ""))
        else:
            title += "   (noch keine Verifikationspaare)"
        ax.set_title(title, fontsize=9, loc="left")
        ax.set_ylim(-0.5, 2.5)
        ax.set_yticks([0, 1, 2], ["OK", "WARN", "NOGO"], fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        if not p:
            continue
        t = [datetime.fromisoformat(q["valid"]) for q in p]
        ax.step(t, [CAT_NUM[q["fcat"]] for q in p], where="mid",
                color="#456990", lw=1.5, alpha=0.8, label="Vorhersage")
        ax.scatter(t, [CAT_NUM[q["ocat"]] for q in p],
                   c=[CAT_COLOR[q["ocat"]] for q in p], zorder=3, s=36,
                   edgecolors="black", linewidths=0.4, label="Beobachtung")
        for ti, q in zip(t, p):
            if q["fcat"] != q["ocat"]:
                ax.plot([ti, ti], [CAT_NUM[q["fcat"]], CAT_NUM[q["ocat"]]],
                        color="#c62828", lw=0.8, ls=":", alpha=0.6)
    np.atleast_1d(axes)[-1].xaxis.set_major_formatter(
        mdates.DateFormatter("%d.%m."))
    np.atleast_1d(axes)[0].legend(loc="upper right", fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    PLOT_FOG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_FOG, dpi=130)
    plt.close(fig)


def render_briefing_plot(bpairs: list[dict], now: datetime) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8))
    fig.suptitle("Briefing-Verifikation (Block-0-Ampeln vs. METAR) — "
                 f"Stand {now:%Y-%m-%d %H:%M} UTC", fontsize=12)
    pcolor = {"FOG": "#6a7fdb", "XW": "#57a773", "CIG": "#c05780"}

    # (1) ACC je Platz und Parameter
    icaos = [a[0] for a in MAIN_AIRPORTS]
    width = 0.25
    x = np.arange(len(icaos))
    for k, param in enumerate(PARAMS):
        accs, ns = [], []
        for icao in icaos:
            s = skill([p for p in bpairs
                       if p["icao"] == icao and p["param"] == param])
            accs.append(s["acc"] if s["acc"] is not None else 0.0)
            ns.append(s["n"])
        bars = ax1.bar(x + (k - 1) * width, accs, width,
                       label=param, color=pcolor[param], alpha=0.9)
        for b, n in zip(bars, ns):
            ax1.annotate(f"n={n}", (b.get_x() + b.get_width() / 2,
                                    b.get_height() + 0.02),
                         ha="center", fontsize=7)
    ax1.set_xticks(x, icaos)
    ax1.set_ylim(0, 1.12)
    ax1.set_ylabel("Trefferquote (exakte Kategorie)")
    ax1.set_title("je Platz und Parameter", fontsize=10, loc="left")
    ax1.grid(True, axis="y", alpha=0.3)
    ax1.legend(fontsize=9)

    # (2) ACC nach Vorlaufzeit
    xb = np.arange(len(LEAD_BUCKETS))
    for k, param in enumerate(PARAMS):
        accs, ns = [], []
        for lo, hi, _lbl in LEAD_BUCKETS:
            s = skill([p for p in bpairs
                       if p["param"] == param and lo <= p["lead_h"] <= hi])
            accs.append(s["acc"] if s["acc"] is not None else 0.0)
            ns.append(s["n"])
        bars = ax2.bar(xb + (k - 1) * width, accs, width,
                       label=param, color=pcolor[param], alpha=0.9)
        for b, n in zip(bars, ns):
            ax2.annotate(f"n={n}", (b.get_x() + b.get_width() / 2,
                                    b.get_height() + 0.02),
                         ha="center", fontsize=7)
    ax2.set_xticks(xb, [b[2] for b in LEAD_BUCKETS])
    ax2.set_ylim(0, 1.12)
    ax2.set_ylabel("Trefferquote (exakte Kategorie)")
    ax2.set_xlabel("Vorlaufzeit der Vorhersage")
    ax2.set_title("alle Plaetze, nach Vorlaufzeit — zeigt den Gueteabfall "
                  "zum 48h-Horizont", fontsize=10, loc="left")
    ax2.grid(True, axis="y", alpha=0.3)

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    PLOT_BRIEF.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_BRIEF, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history: dict = {"forecasts": [], "observations": [],
                     "briefing_forecasts": [], "ingested_briefings": []}
    if HISTORY.exists():
        history.update(json.loads(HISTORY.read_text()))

    async with httpx.AsyncClient(follow_redirects=True) as client:
        history["observations"] += await fetch_observations(client, now)
        history["forecasts"] += await fetch_forecasts(client, now)
    harvested = harvest_briefings(history)

    fog_pairs = match_fog_pairs(history, now)
    bpairs = match_briefing_pairs(history, now)
    render_fog_plot(fog_pairs, now)
    render_briefing_plot(bpairs, now)

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=1))

    print(f"Historie: {len(history['forecasts'])} Direkt-Forecasts, "
          f"{len(history['briefing_forecasts'])} Briefing-Zeilen "
          f"(+{harvested} neu geerntet), "
          f"{len(history['observations'])} Beobachtungen")
    print(f"Paare: Nebel(+24h)={len(fog_pairs)}, Briefing={len(bpairs)}")
    for param in PARAMS:
        s = skill([p for p in bpairs if p["param"] == param])
        if s["n"]:
            print(f"  {param}: n={s['n']} ACC={s['acc']:.0%}"
                  + (f" POD={s['pod']:.0%}" if s["pod"] is not None else "")
                  + (f" FAR={s['far']:.0%}" if s["far"] is not None else ""))


if __name__ == "__main__":
    asyncio.run(main())
