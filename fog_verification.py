"""
fog_verification.py
===================

Verifikation der CAPS-Nebelvorhersage gegen eingetretene METAR-Beobachtungen.

Taeglicher Ablauf (GitHub Actions, 1x/24h):
  1. Aktuelle METARs der Hauptplaetze holen -> beobachtete Nebelkategorie
     (NOGO: Sicht < 1.6 km oder FG/FZFG; WARN: < 5 km oder BR; sonst OK)
  2. CAPS-2m-Spread fuer jetzt+24h holen -> vorhergesagte Kategorie
     (NOGO <= 1.5 °C; WARN <= 3.0 °C; OK darueber)
  3. Faellige Vorhersagen (valid <= jetzt) mit Beobachtungen paaren (±3 h)
  4. Kennzahlen je Platz: ACC (Kategorie-Treffer), POD (erkannte
     Nebelereignisse), FAR (Fehlalarmquote); Nebelereignis = WARN oder NOGO
  5. Grafik: verification/fog_verification.png (7 Panels, Vorhersage als
     Linie, Beobachtung als Punkte, gruen/orange/rot)

Historie: verification/history.json (Forecasts + Observations, append-only)

Abhaengigkeiten: pip install httpx xarray cfgrib numpy matplotlib
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

CAPS_BASE = "https://dd.weather.gc.ca/today/model_caps/3km"
AWC_API = "https://aviationweather.gov/api/data"
HISTORY = Path("verification/history.json")
PLOT = Path("verification/fog_verification.png")

MAIN_AIRPORTS = [
    ("CYFB", "Iqaluit",       63.756,  -68.556),
    ("CYIO", "Pond Inlet",    72.683,  -77.967),
    ("CYRB", "Resolute Bay",  74.717,  -94.969),
    ("CYHK", "Gjoa Haven",    68.636,  -95.850),
    ("CYCB", "Cambridge Bay", 69.108, -105.138),
    ("PABR", "Utqiagvik",     71.285, -156.766),
    ("PAOM", "Nome",          64.512, -165.445),
]

CAT_NUM = {"OK": 0, "WARN": 1, "NOGO": 2}
CAT_COLOR = {"OK": "#2e8b57", "WARN": "#e69500", "NOGO": "#c62828"}
MATCH_TOLERANCE_H = 3.0


# ---------------------------------------------------------------------------
# Kategorisierung
# ---------------------------------------------------------------------------

def cat_from_spread(spread_c: float) -> str:
    return "NOGO" if spread_c <= 1.5 else ("WARN" if spread_c <= 3.0 else "OK")


def parse_metar_category(raw: str) -> tuple[float | None, str]:
    """(Sicht km oder None, Kategorie) aus rohem METAR-Text."""
    vis_km = None
    m = re.search(r"\b(?:M)?(\d+)?(?:\s+)?(\d)/(\d)SM\b", raw)
    if m:                                    # Bruch, ggf. mit Ganzzahl davor
        whole = int(m.group(1)) if m.group(1) else 0
        vis_km = (whole + int(m.group(2)) / int(m.group(3))) * 1.609
    else:
        m = re.search(r"\b(\d+)SM\b", raw)
        if m:
            vis_km = int(m.group(1)) * 1.609
    fog = re.search(r"\b(?:\+|-)?(?:FZ)?FG\b", raw) is not None
    mist = re.search(r"\bBR\b", raw) is not None
    if fog or (vis_km is not None and vis_km < 1.6):
        return vis_km, "NOGO"
    if mist or (vis_km is not None and vis_km < 5.0):
        return vis_km, "WARN"
    return vis_km, "OK"


# ---------------------------------------------------------------------------
# Datenerfassung
# ---------------------------------------------------------------------------

async def fetch_observations(client: httpx.AsyncClient, now: datetime) -> list[dict]:
    ids = ",".join(a[0] for a in MAIN_AIRPORTS)
    try:
        r = await client.get(f"{AWC_API}/metar",
                             params={"ids": ids, "format": "raw"},
                             headers={"User-Agent": "fog-verification/1.0"},
                             timeout=45.0)
        raw = r.text if r.status_code == 200 else ""
    except httpx.HTTPError:
        raw = ""
    obs = []
    for line in raw.splitlines():
        line = line.strip()
        for icao, *_ in MAIN_AIRPORTS:
            if line.startswith(icao) or line.startswith(f"METAR {icao}") \
                    or line.startswith(f"SPECI {icao}"):
                vis, cat = parse_metar_category(line)
                obs.append({"time": now.isoformat(), "icao": icao,
                            "vis_km": vis, "cat": cat, "raw": line[:120]})
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
    if not 1 <= fh <= 48:
        print(f"fh={fh} ausserhalb 1..48 — Lauf/Zeitfenster pruefen",
              file=sys.stderr)
        return []
    url = (f"{CAPS_BASE}/{run}/{fh:03d}/{date}T{run}Z_MSC_CAPS_"
           f"DewPointDepression_AGL-2m_RLatLon0.03_PT{fh:03d}H.grib2")
    try:
        r = await client.get(url, timeout=90.0)
        if r.status_code != 200:
            # Fallback: vorheriger Lauf, fh + 12
            fb = run_dt - timedelta(hours=12)
            date, run, run_dt = fb.strftime("%Y%m%d"), f"{fb.hour:02d}", fb
            fh += 12
            if fh > 48:
                return []
            url = (f"{CAPS_BASE}/{run}/{fh:03d}/{date}T{run}Z_MSC_CAPS_"
                   f"DewPointDepression_AGL-2m_RLatLon0.03_PT{fh:03d}H.grib2")
            r = await client.get(url, timeout=90.0)
            if r.status_code != 200:
                print("CAPS-Spread nicht abrufbar", file=sys.stderr)
                return []
    except httpx.HTTPError as exc:
        print(f"CAPS-Abruf fehlgeschlagen: {exc}", file=sys.stderr)
        return []

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(r.content)
        gpath = Path(tf.name)
    try:
        ds = xr.open_dataset(gpath, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        lat2d = ds["latitude"].values
        lon2d = ds["longitude"].values
        lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
        field = ds[next(iter(ds.data_vars))].values.squeeze()
        out = []
        vt = run_dt + timedelta(hours=fh)
        for icao, _name, lat, lon in MAIN_AIRPORTS:
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
# Paarung und Kennzahlen
# ---------------------------------------------------------------------------

def match_pairs(history: dict, now: datetime) -> list[dict]:
    """Faellige Forecasts mit zeitlich naechster Beobachtung paaren."""
    obs_by_icao: dict[str, list[dict]] = {}
    for o in history["observations"]:
        obs_by_icao.setdefault(o["icao"], []).append(o)
    pairs = []
    for f in history["forecasts"]:
        valid = datetime.fromisoformat(f["valid"])
        if valid > now:
            continue
        best, best_dt = None, MATCH_TOLERANCE_H
        for o in obs_by_icao.get(f["icao"], []):
            dt = abs((datetime.fromisoformat(o["time"]) - valid)
                     .total_seconds()) / 3600
            if dt < best_dt:
                best, best_dt = o, dt
        if best:
            pairs.append({"icao": f["icao"], "valid": f["valid"],
                          "fcat": f["cat"], "ocat": best["cat"],
                          "spread": f["spread"], "vis_km": best["vis_km"]})
    return pairs


def skill(pairs: list[dict]) -> dict:
    """ACC, POD, FAR; Nebelereignis = Kategorie WARN oder NOGO."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "acc": None, "pod": None, "far": None}
    acc = sum(p["fcat"] == p["ocat"] for p in pairs) / n
    obs_fog = [p for p in pairs if p["ocat"] != "OK"]
    fc_fog = [p for p in pairs if p["fcat"] != "OK"]
    hits = sum(p["fcat"] != "OK" for p in obs_fog)
    pod = hits / len(obs_fog) if obs_fog else None
    far = (sum(p["ocat"] == "OK" for p in fc_fog) / len(fc_fog)
           if fc_fog else None)
    return {"n": n, "acc": acc, "pod": pod, "far": far}


# ---------------------------------------------------------------------------
# Grafik
# ---------------------------------------------------------------------------

def render_plot(pairs: list[dict], now: datetime) -> None:
    fig, axes = plt.subplots(len(MAIN_AIRPORTS), 1, sharex=True,
                             figsize=(11, 2.0 * len(MAIN_AIRPORTS)))
    if len(MAIN_AIRPORTS) == 1:
        axes = [axes]
    fig.suptitle("CAPS-Nebelvorhersage (+24 h) vs. METAR-Beobachtung — "
                 f"Stand {now:%Y-%m-%d %H:%M} UTC\n"
                 "Linie = Vorhersage, Punkte = Beobachtung  |  "
                 "0=OK 1=WARN 2=NOGO", fontsize=11)

    for ax, (icao, name, *_coords) in zip(axes, MAIN_AIRPORTS):
        p = sorted((q for q in pairs if q["icao"] == icao),
                   key=lambda q: q["valid"])
        s = skill(p)
        title = f"{icao} {name}"
        if s["n"]:
            title += (f"   n={s['n']}  ACC={s['acc']:.0%}"
                      + (f"  POD={s['pod']:.0%}" if s["pod"] is not None
                         else "  POD=—")
                      + (f"  FAR={s['far']:.0%}" if s["far"] is not None
                         else "  FAR=—"))
        else:
            title += "   (noch keine Verifikationspaare)"
        ax.set_title(title, fontsize=9, loc="left")
        ax.set_ylim(-0.5, 2.5)
        ax.set_yticks([0, 1, 2], ["OK", "WARN", "NOGO"], fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        if not p:
            continue
        t = [datetime.fromisoformat(q["valid"]) for q in p]
        fnum = [CAT_NUM[q["fcat"]] for q in p]
        onum = [CAT_NUM[q["ocat"]] for q in p]
        ax.step(t, fnum, where="mid", color="#456990", lw=1.5,
                alpha=0.8, label="Vorhersage")
        ax.scatter(t, onum, c=[CAT_COLOR[q["ocat"]] for q in p],
                   zorder=3, s=36, edgecolors="black", linewidths=0.4,
                   label="Beobachtung")
        # Fehltreffer markieren
        for ti, q in zip(t, p):
            if q["fcat"] != q["ocat"]:
                ax.plot([ti, ti], [CAT_NUM[q["fcat"]], CAT_NUM[q["ocat"]]],
                        color="#c62828", lw=0.8, ls=":", alpha=0.6)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m."))
    axes[0].legend(loc="upper right", fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history = {"forecasts": [], "observations": []}
    if HISTORY.exists():
        history = json.loads(HISTORY.read_text())

    async with httpx.AsyncClient(follow_redirects=True) as client:
        history["observations"] += await fetch_observations(client, now)
        history["forecasts"] += await fetch_forecasts(client, now)

    pairs = match_pairs(history, now)
    render_plot(pairs, now)

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=1))

    total = skill(pairs)
    print(f"Historie: {len(history['forecasts'])} Forecasts, "
          f"{len(history['observations'])} Beobachtungen, "
          f"{total['n']} Verifikationspaare")
    if total["n"]:
        print(f"Gesamt: ACC={total['acc']:.0%}"
              + (f" POD={total['pod']:.0%}" if total["pod"] is not None else "")
              + (f" FAR={total['far']:.0%}" if total["far"] is not None else ""))


if __name__ == "__main__":
    asyncio.run(main())
