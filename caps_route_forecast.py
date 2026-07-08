"""
caps_route_forecast.py — v4
===========================

Wetterbriefing Nordwestpassage (DA62) mit Machbarkeits-Dashboard.

  Block 0: MACHBARKEIT   — Ampelbewertung pro Platz und Zeitfenster
  Block 1: CAPS 3 km     — Rohdaten Wind/Temp/Spread/Bedeckung, 48 h
  Block 2: RDPS-WEonG    — Nebelsicht 10 km, bis 84 h
  Block 3: TAF/METAR     — amtliche Meldungen (aviationweather.gov)

Ampelkriterien (Block 0), Bewertung in 6-h-Schritten bis +48 h:
  HW  Headwind-Komponente in 8000 ft entlang des Anflugkurses
      (Interpolation 850/700 hPa):      G < 10 kt | O 10-20 | R > 20
  XW  Crosswind aus Boeen (10 m) zur Piste:  G <= 10 kt | O 10-20 | R > 20
      (Piste unbekannt -> volle Boe, konservativ, mit '~' markiert)
  CIG Wolkenbasis (niedrigste Druckflaeche RH>=90%, barometrisch in
      ft AGL umgerechnet):   G >= 5000 ft | O 2000-5000 | R < 2000
  SP  min. Taupunkt-Spread 850/700 hPa:  R <= 1.5 K | O 1.5-3 | G > 3
  Gesamt = schlechteste Einzelwertung.

Abhaengigkeiten: pip install httpx xarray cfgrib numpy   (+ ecCodes)
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import xarray as xr

CAPS_BASE = "https://dd.weather.gc.ca/today/model_caps/3km"
RDPS_WEONG_BASES = [
    "https://dd.alpha.weather.gc.ca/model_rdps/10km",
    "https://dd.alpha.weather.gc.ca/model_gem_regional/10km",
    "https://dd.weather.gc.ca/today/model_rdps/10km",
]
AWC_API = "https://aviationweather.gov/api/data"

MS_TO_KT = 1.9438
MAX_GRID_DIST_DEG = 0.10

# ---------------------------------------------------------------------------
# Route, Ausweichplaetze, Pisten
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Waypoint:
    icao: str
    name: str
    lat: float
    lon: float
    role: str = "RTE"      # RTE oder ALT
    origin: str = ""       # ICAO des Anflug-Ausgangspunkts (Kursberechnung)

ROUTE: list[Waypoint] = [
    Waypoint("CYFB", "Iqaluit",        63.756,  -68.556, "RTE", ""),
    Waypoint("CYIO", "Pond Inlet",     72.683,  -77.967, "RTE", "CYFB"),
    Waypoint("CYRB", "Resolute Bay",   74.717,  -94.969, "RTE", "CYIO"),
    Waypoint("CYHK", "Gjoa Haven",     68.636,  -95.850, "RTE", "CYRB"),
    Waypoint("CYCB", "Cambridge Bay",  69.108, -105.138, "RTE", "CYHK"),
    Waypoint("PABR", "Utqiagvik",      71.285, -156.766, "RTE", "CYCB"),
    Waypoint("PAOM", "Nome",           64.512, -165.445, "RTE", "PABR"),
    Waypoint("CYCY", "Clyde River",    70.486,  -68.517, "ALT", "CYFB"),
    Waypoint("CYAB", "Arctic Bay",     73.006,  -85.047, "ALT", "CYIO"),
    Waypoint("CYYH", "Taloyoak",       69.547,  -93.577, "ALT", "CYRB"),
    Waypoint("CYBB", "Kugaaruk",       68.534,  -89.808, "ALT", "CYRB"),
    Waypoint("CYCO", "Kugluktuk",      67.817, -115.144, "ALT", "CYCB"),
    Waypoint("CYHI", "Ulukhaktok",     70.763, -117.806, "ALT", "CYCB"),
    Waypoint("CYPC", "Paulatuk",       69.361, -124.075, "ALT", "CYCB"),
    Waypoint("CYUB", "Tuktoyaktuk",    69.433, -133.026, "ALT", "CYCB"),
    Waypoint("CYEV", "Inuvik",         68.304, -133.483, "ALT", "CYCB"),
    Waypoint("PASC", "Deadhorse",      70.195, -148.465, "ALT", "CYCB"),
    Waypoint("PAWI", "Wainwright",     70.638, -159.995, "ALT", "PABR"),
    Waypoint("PAPO", "Point Hope",     68.349, -166.799, "ALT", "PABR"),
    Waypoint("PAOT", "Kotzebue",       66.885, -162.599, "ALT", "PABR"),
]

# Pistenrichtung RECHTWEISEND (eine Richtung genuegt, Gegenrichtung implizit).
# NDA-Plaetze: Pistennummer ist bereits rechtweisend. Alaska: Deklination
# eingerechnet. None = unbekannt -> konservativ volle Boe als Crosswind.
# >>> VOR ABFLUG GEGEN CFS (Kanada) BZW. CHART SUPPLEMENT (Alaska) PRUEFEN <<<
RUNWAY_TRUE: dict[str, float | None] = {
    "CYFB": 160.0,   # RWY 16T/34T
    "CYIO": 20.0,    # RWY 02T/20T
    "CYRB": 170.0,   # RWY 17T/35T
    "CYHK": 130.0,   # RWY 13/31 (NDA true)
    "CYCB": 130.0,   # RWY 13T/31T
    "PABR": 82.0,    # RWY 07/25 mag + ~12E Var
    "PAOM": 119.0,   # RWY 10/28 mag + ~9E Var (zweite Piste 03/21 vorhanden)
    "CYCY": 20.0,    # RWY 02/20 (NDA true)
    "CYAB": 130.0,   # RWY 13/31 (NDA true)
    "CYYH": 150.0,   # RWY 15/33 (NDA true)
    "CYBB": 50.0,    # RWY 05/23 (NDA true)
    "CYCO": 120.0,   # RWY 12/30 (NDA true)
    "CYHI": 60.0,    # RWY 06T/24T
    "CYPC": 20.0,    # RWY 02T/20T
    "CYUB": 100.0,   # RWY 10/28 (NDA true)
    "CYEV": 60.0,    # RWY 06T/24T
    "PASC": 65.0,    # RWY 05/23 mag + ~15E Var
    "PAWI": 62.0,    # RWY 05/23 mag + ~12E Var (Gravel)
    "PAPO": 20.0,    # RWY 01/19 mag + ~10E Var
    "PAOT": 99.0,    # RWY 09/27 mag + ~9E Var (zweite Piste 18/36 Gravel)
}

# CAPS-Variablen. RH-Niedriglevels + Bodendruck fuer die Ceiling-Ableitung.
CEILING_LEVELS = ["1015", "1000", "0985", "0970", "0950",
                  "0925", "0900", "0875", "0850"]
CAPS_VARIABLES: list[tuple[str, str]] = ([
    ("WindSpeed",          "IsbL-0700"),
    ("WindDir",            "IsbL-0700"),
    ("WindSpeed",          "IsbL-0850"),
    ("WindDir",            "IsbL-0850"),
    ("AirTemp",            "IsbL-0700"),
    ("AirTemp",            "IsbL-0850"),
    ("AirTemp",            "AGL-2m"),
    ("DewPointDepression", "IsbL-0700"),
    ("DewPointDepression", "IsbL-0850"),
    ("DewPointDepression", "AGL-2m"),
    ("WindGust",           "AGL-10m"),
    ("WindSpeed",          "AGL-10m"),
    ("WindDir",            "AGL-10m"),
    ("TotalCloudCover",    "Sfc"),
    ("Pressure",           "Sfc"),
] + [("RelativeHumidity", f"IsbL-{l}") for l in CEILING_LEVELS])

CAPS_HOURS = range(3, 49, 3)
DASH_HOURS = range(6, 49, 6)
FOG_HOURS = list(range(3, 49, 3)) + list(range(54, 85, 6))
TRD_DELTA_CAP = 5.0     # max. Delta °C/6h in der Trend-Projektion (Tagesgang)
TRD_DRY_SPREAD = 5.0    # Spread darueber: TRD hoechstens WARN, nie NOGO


# ---------------------------------------------------------------------------
# Basis-Helfer
# ---------------------------------------------------------------------------

def latest_expected_run(latency_h: int) -> tuple[str, str]:
    c = datetime.now(timezone.utc) - timedelta(hours=latency_h)
    return c.strftime("%Y%m%d"), "12" if c.hour >= 12 else "00"


def previous_run(date: str, run: str) -> tuple[str, str]:
    fb = (datetime.strptime(date + run, "%Y%m%d%H")
          .replace(tzinfo=timezone.utc) - timedelta(hours=12))
    return fb.strftime("%Y%m%d"), f"{fb.hour:02d}"


def initial_bearing(lat1, lon1, lat2, lon2) -> float:
    """Rechtweisender Anfangskurs Grosskreis, Grad."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return math.degrees(math.atan2(x, y)) % 360


async def fetch(client, url: str, dest: Path) -> Path | None:
    try:
        r = await client.get(url, timeout=90.0)
        if r.status_code != 200:
            print(f"  [404] {url.rsplit('/', 1)[-1]}", file=sys.stderr)
            return None
        dest.write_bytes(r.content)
        return dest
    except httpx.HTTPError as exc:
        print(f"  [ERR] {url.rsplit('/', 1)[-1]}: {exc}", file=sys.stderr)
        return None


def extract_points(grib_path: Path,
                   points: list[tuple[float, float]]) -> list[float] | None:
    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        lat2d = ds["latitude"].values
        lon2d = ds["longitude"].values
        lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
        field = ds[next(iter(ds.data_vars))].values.squeeze()
        out = []
        for lat, lon in points:
            d2 = ((lat2d - lat) ** 2
                  + ((lon2d - lon) * math.cos(math.radians(lat))) ** 2)
            j, i = np.unravel_index(np.argmin(d2), d2.shape)
            out.append(math.nan if math.sqrt(d2[j, i]) > MAX_GRID_DIST_DEG
                       else float(field[j, i]))
        ds.close()
        return out
    except Exception as exc:
        print(f"  [PARSE] {grib_path.name}: {exc}", file=sys.stderr)
        return None


def valid(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


# ---------------------------------------------------------------------------
# CAPS-Daten laden (gemeinsame Basis fuer Block 0 und 1)
# ---------------------------------------------------------------------------

async def load_caps(client, tmpdir: Path, waypoints
                    ) -> tuple[dict, str, str]:
    date, run = latest_expected_run(7)   # Publikation gemessen: Lauf + ~6.6 h
    probe = (f"{CAPS_BASE}/{run}/{CAPS_HOURS[-1]:03d}/{date}T{run}Z_MSC_CAPS_"
             f"AirTemp_AGL-2m_RLatLon0.03_PT{CAPS_HOURS[-1]:03d}H.grib2")
    if (await client.head(probe, timeout=30.0)).status_code != 200:
        date, run = previous_run(date, run)
        print(f"CAPS: Fallback auf Lauf {date} {run}Z", file=sys.stderr)

    points = [(w.lat, w.lon) for w in waypoints]
    data: dict[int, dict[str, list[float] | None]] = {}
    for fh in CAPS_HOURS:
        async def one(var, lvl, fh=fh):
            url = (f"{CAPS_BASE}/{run}/{fh:03d}/{date}T{run}Z_MSC_CAPS_"
                   f"{var}_{lvl}_RLatLon0.03_PT{fh:03d}H.grib2")
            p = await fetch(client, url, tmpdir / f"c{var}{lvl}{fh}.grib2")
            return f"{var}_{lvl}", p
        res = await asyncio.gather(*(one(v, l) for v, l in CAPS_VARIABLES))
        data[fh] = {k: (extract_points(p, points) if p else None)
                    for k, p in res}
        for _, p in res:
            if p:
                p.unlink(missing_ok=True)
    return data, date, run


def gv(data, fh, key, wi):
    vals = data.get(fh, {}).get(key)
    return vals[wi] if vals is not None else None


# ---------------------------------------------------------------------------
# Ampellogik
# ---------------------------------------------------------------------------

def wind_uv(spd, direc):
    """u/v (m/s) aus meteorologischer Richtung/Geschwindigkeit."""
    r = math.radians(direc)
    return -spd * math.sin(r), -spd * math.cos(r)


def headwind_8000(data, fh, wi, course) -> float | None:
    s7, d7 = (gv(data, fh, "WindSpeed_IsbL-0700", wi),
              gv(data, fh, "WindDir_IsbL-0700", wi))
    s8, d8 = (gv(data, fh, "WindSpeed_IsbL-0850", wi),
              gv(data, fh, "WindDir_IsbL-0850", wi))
    if not all(valid(x) for x in (s7, d7, s8, d8)):
        return None
    w = 0.63                                # 8000 ft zwischen 850 und 700
    u7, v7 = wind_uv(s7, d7)
    u8, v8 = wind_uv(s8, d8)
    u, v = (1 - w) * u8 + w * u7, (1 - w) * v8 + w * v7
    cr = math.radians(course)
    # Headwind positiv, wenn Windkomponente dem Kurs entgegensteht
    return -(u * math.sin(cr) + v * math.cos(cr)) * MS_TO_KT


def crosswind_gust(data, fh, wi, rwy_true) -> tuple[float | None, bool]:
    """(Crosswind kt, exakt?) — ohne Piste: volle Boe, exakt=False."""
    g = gv(data, fh, "WindGust_AGL-10m", wi)
    d = gv(data, fh, "WindDir_AGL-10m", wi)
    if not valid(g):
        return None, True
    g_kt = g * MS_TO_KT
    if rwy_true is None or not valid(d):
        return g_kt, False
    return g_kt * abs(math.sin(math.radians(d - rwy_true))), True


def ceiling_ft(data, fh, wi) -> float | None:
    """Wolkenbasis: 90%-RH-Durchgang zwischen Druckflaechen interpoliert,
    barometrisch in ft AGL. Bedeckungs-Veto: TotalCloudCover < 50% kann
    kein BKN/OVC tragen -> kein Ceiling. 99999 = frei unterhalb ~5000 ft.
    Ausgabe ist eine Schaetzung (Anzeige gerundet auf 100 ft)."""
    p_sfc = gv(data, fh, "Pressure_Sfc", wi)          # Pa
    if not valid(p_sfc):
        return None
    prev_p, prev_rh = None, None
    for lvl in CEILING_LEVELS:                        # hoechster Druck zuerst
        p_lvl = float(lvl) * 100.0
        if p_lvl >= p_sfc:
            continue                                  # Level "unter Grund"
        rh = gv(data, fh, f"RelativeHumidity_IsbL-{lvl}", wi)
        if not valid(rh):
            continue
        if rh >= 90.0:
            tcc = gv(data, fh, "TotalCloudCover_Sfc", wi)
            if valid(tcc) and tcc < 50.0:
                return 99999.0        # gesaettigte Flaeche, aber kein Deck
            if prev_rh is not None and prev_rh < 90.0:
                f = (90.0 - prev_rh) / (rh - prev_rh)
                p_base = prev_p + f * (p_lvl - prev_p)
            else:
                p_base = p_lvl        # schon unterste Flaeche gesaettigt
            return 8000.0 * math.log(p_sfc / p_base) * 3.281
        prev_p, prev_rh = p_lvl, rh
    return 99999.0


def fog_spread_2m(data, fh, wi) -> float | None:
    """Taupunkt-Spread 2 m (°C) — Indikator Bodennebel."""
    return gv(data, fh, "DewPointDepression_AGL-2m", wi)


def fog_trend(data, fh, wi) -> tuple[float | None, str]:
    """Trend des 2-m-Spreads: SP-Schwellen auf +6 h extrapoliert.

    d      = Spread(fh) - Spread(fh-6) [erste Zeile: 2 x 3-h-Delta]
    |d|<0.3 gilt als stabil (Modellrauschen).
    Klasse = cls_sp(Spread + d_gedaempft), Anzeige = ungedaempftes d.

    Daempfer gegen Tagesgang-Fehlalarme:
      1. In die Projektion geht hoechstens ±TRD_DELTA_CAP °C/6h ein —
         groessere Spruenge sind Tagesgang, nicht Nebelentwicklung.
      2. Liegt der aktuelle Spread ueber TRD_DRY_SPREAD, ist die Klasse
         auf WARN gedeckelt: aus sehr trockener Luft ist ein Nebel-NOGO
         binnen 6 h physikalisch unglaubwuerdig.
    """
    sp_now = fog_spread_2m(data, fh, wi)
    if fh - 6 in data:
        sp_prev, scale = fog_spread_2m(data, fh - 6, wi), 1.0
    elif fh - 3 in data:
        sp_prev, scale = fog_spread_2m(data, fh - 3, wi), 2.0
    else:
        return None, "?"
    if not (valid(sp_now) and valid(sp_prev)):
        return None, "?"
    d6 = (sp_now - sp_prev) * scale
    if abs(d6) < 0.3:
        d6 = 0.0
    d6_proj = max(min(d6, TRD_DELTA_CAP), -TRD_DELTA_CAP)
    cls = cls_sp(max(sp_now + d6_proj, 0.0))
    if d6 < -TRD_DELTA_CAP and cls == "OK":
        cls = "WARN"            # extremer Kollaps: mindestens beobachten
    if sp_now > TRD_DRY_SPREAD and cls == "NOGO":
        cls = "WARN"
    return d6, cls


def icing_assess(data, fh, wi) -> tuple[float | None, str]:
    """Vereisung: Spread UND Temperatur auf 850/700 hPa kombiniert.

    NOGO: Spread <= 1.5 °C bei T <= 0 °C (in der Wolke, unterkuehlt)
    WARN: Spread <= 3.0 °C bei T <= 0 °C
    OK:   sonst (warme Schicht oder wolkenfrei)
    Rueckgabe: (min. Spread der unterkuehlten Level oder None, Klasse).
    """
    worst, val, seen = "OK", None, False
    for lvl in ("0850", "0700"):
        t = gv(data, fh, f"AirTemp_IsbL-{lvl}", wi)
        sp = gv(data, fh, f"DewPointDepression_IsbL-{lvl}", wi)
        if not (valid(t) and valid(sp)):
            continue
        seen = True
        if t <= 273.15:
            val = sp if val is None else min(val, sp)
            if sp <= 1.5:
                worst = "NOGO"
            elif sp <= 3.0 and worst != "NOGO":
                worst = "WARN"
    if not seen:
        return None, "?"
    return val, worst


def cls_hw(v):  return "OK" if v < 10 else ("WARN" if v <= 20 else "NOGO")
def cls_xw(v):  return "OK" if v <= 10 else ("WARN" if v <= 20 else "NOGO")
def cls_cig(v): return "OK" if v >= 5000 else ("WARN" if v >= 2000 else "NOGO")
def cls_sp(v):  return "NOGO" if v <= 1.5 else ("WARN" if v <= 3.0 else "OK")

WORST = {"OK": 0, "WARN": 1, "NOGO": 2, "?": 1}
LABEL = {0: "OK", 1: "WARN", 2: "NOGO"}


def dashboard_block(data, date, run, waypoints, obs=None) -> list[str]:
    obs = obs or {}
    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    coords = {w.icao: w for w in waypoints}
    lines = [
        "BLOCK 0 — MACHBARKEIT (personalisierte Bewertung, kein amtliches "
        "Produkt)", "=" * 70,
        "Bewertung je Parameter: OK / WARN / NOGO  (? = Daten fehlen)  |  "
        "Gesamt = schlechteste Einzelwertung",
        "HW: Headwind 8000ft im Anflugkurs | XW: Crosswind aus Boeen "
        "(~ = Piste unbekannt, volle Boe) | CIG: Wolkenbasis ft AGL "
        "(RH-Interpolation, auf 100 ft gerundet; nur bei Bedeckung >=50%) | "
        "SP2m: Taupunkt-Spread 2m in °C (Bodennebel: NOGO<=1.5, WARN<=3) | "
        "TRD: Spread-Trend °C/6h (Klasse = SP-Schwellen auf +6h "
        "extrapoliert; negativ = schliessend) | "
        "ICE: Spread+Temp 850/700 (nur unterkuehlte Level; '—' = alle "
        "Level >0°C)",
        "* = durch aktuelles METAR herabgestuft (Persistenz: voll bis +6h, "
        "abgeschwaecht bis +12h ab Beobachtung; nur verschaerfend)", ""]

    for wi, wp in enumerate(waypoints):
        # Kurs vom Anflug-Ausgangspunkt
        course = None
        if wp.origin and wp.origin in coords:
            o = coords[wp.origin]
            course = initial_bearing(o.lat, o.lon, wp.lat, wp.lon)
        tag = "[ALT] " if wp.role == "ALT" else ""
        hdr = f"--- {wp.icao} {wp.name} {tag}"
        hdr += (f"(Anflug {wp.origin}, TC {course:03.0f}) "
                if course is not None else "(kein Anflugkurs) ")
        rwy = RUNWAY_TRUE.get(wp.icao)
        hdr += f"RWY {rwy:03.0f}T ---" if rwy is not None else "RWY unbek. ---"
        lines.append(hdr)

        probe = gv(data, DASH_HOURS[0], "AirTemp_AGL-2m", wi)
        if probe is not None and isinstance(probe, float) and math.isnan(probe):
            lines.append("    ausserhalb der CAPS-Domain — Bewertung nicht "
                         "moeglich (siehe Block 2/3)\n")
            continue

        lines.append(f"{'VT (UTC)':<12}{'HW kt':>12}{'XW kt':>13}"
                     f"{'CIG ft':>14}{'SP2m °C':>12}{'TRD 6h':>12}"
                     f"{'ICE °C':>12}   GESAMT")
        entry = obs.get(wp.icao)
        downgraded = False
        for fh in DASH_HOURS:
            vt = run_dt + timedelta(hours=fh)
            fog_floor, cig_floor = persistence_floor(entry, vt)
            hw = headwind_8000(data, fh, wi, course) \
                if course is not None else None
            xw, exact = crosswind_gust(data, fh, wi, rwy)
            cig = ceiling_ft(data, fh, wi)
            sp = fog_spread_2m(data, fh, wi)
            trd_val, trd_cls = fog_trend(data, fh, wi)
            ice_val, ice_cls = icing_assess(data, fh, wi)

            parts, worst = [], 0
            def one(v, cls, fmt, mark="", floor=None):
                nonlocal worst, downgraded
                if not valid(v):
                    parts.append(f"{'?':>11}")
                    worst = max(worst, WORST["?"])
                else:
                    c = cls(v)
                    if floor is not None and WORST[floor] > WORST[c]:
                        c = floor
                        mark += "*"
                        downgraded = True
                    worst = max(worst, WORST[c])
                    parts.append(f"{fmt(v)}{mark} {c:<4}")
            one(hw, cls_hw, lambda v: f"{v:+6.0f}") if course is not None \
                else (parts.append(f"{'—':>11}"))
            one(xw, cls_xw, lambda v: f"{v:6.0f}", "" if exact else "~")
            one(cig, cls_cig,
                lambda v: ("  >5000" if v >= 99999
                           else f"~{round(v / 100) * 100:5.0f}"),
                floor=cig_floor)
            one(sp, cls_sp, lambda v: f"{v:5.1f}", floor=fog_floor)
            # TRD-Spalte: Klasse aus fog_trend (Projektion), Anzeige = Delta
            if trd_cls == "?":
                parts.append(f"{'?':>11}")
                worst = max(worst, WORST["?"])
            else:
                worst = max(worst, WORST[trd_cls])
                parts.append(f"{trd_val:+5.1f} {trd_cls:<4}")
            # ICE-Spalte: Klasse kommt aus icing_assess, nicht aus Schwellen
            if ice_cls == "?":
                parts.append(f"{'?':>11}")
                worst = max(worst, WORST["?"])
            else:
                worst = max(worst, WORST[ice_cls])
                disp = f"{ice_val:5.1f}" if ice_val is not None else f"{'—':>5}"
                parts.append(f"{disp} {ice_cls:<4}")
            lines.append(f"{vt:%d. %H}Z     " + " ".join(parts)
                         + f"   [{LABEL[worst]}]")
        if downgraded and entry is not None:
            lines.append(f"  * Persistenz-Korrektur aktiv — METAR "
                         f"{entry['time']:%d. %H:%M}Z: "
                         f"{entry['raw'][:70]}")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Block 1: CAPS-Rohdaten (nutzt bereits geladene Daten)
# ---------------------------------------------------------------------------

def caps_block(data, date, run, waypoints) -> list[str]:
    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"\n\nBLOCK 1 — CAPS 3 km Rohdaten, Lauf {date} {run}Z (48 h)",
             "=" * 70]
    for wi, wp in enumerate(waypoints):
        tag = " [ALT]" if wp.role == "ALT" else ""
        lines.append(f"\n=== {wp.icao} {wp.name}{tag} ===")
        probe = gv(data, CAPS_HOURS[0], "AirTemp_AGL-2m", wi)
        if probe is not None and isinstance(probe, float) and math.isnan(probe):
            lines.append("    ausserhalb der CAPS-Domain")
            continue
        lines.append(f"{'VT (UTC)':<13}{'W700':<11}{'W850':<11}"
                     f"{'T700/Sp':<11}{'T850/Sp':<11}{'T2m':<7}"
                     f"{'Wind10m':<11}{'Böen':<7}{'Bew.'}")
        for fh in CAPS_HOURS:
            def wind(sk, dk):
                s, d = gv(data, fh, sk, wi), gv(data, fh, dk, wi)
                return (f"{d:03.0f}/{s * MS_TO_KT:.0f}kt"
                        if valid(s) and valid(d) else "n/a")
            def tsp(tk, sk):
                t, sp = gv(data, fh, tk, wi), gv(data, fh, sk, wi)
                if not valid(t):
                    return "n/a"
                base = f"{t - 273.15:+.0f}"
                return f"{base}/{sp:.0f}K" if valid(sp) else base
            t2m = gv(data, fh, "AirTemp_AGL-2m", wi)
            gu = gv(data, fh, "WindGust_AGL-10m", wi)
            cc = gv(data, fh, "TotalCloudCover_Sfc", wi)
            vt = run_dt + timedelta(hours=fh)
            lines.append(
                f"{vt:%d. %H}Z      "
                f"{wind('WindSpeed_IsbL-0700', 'WindDir_IsbL-0700'):<11}"
                f"{wind('WindSpeed_IsbL-0850', 'WindDir_IsbL-0850'):<11}"
                f"{tsp('AirTemp_IsbL-0700', 'DewPointDepression_IsbL-0700'):<11}"
                f"{tsp('AirTemp_IsbL-0850', 'DewPointDepression_IsbL-0850'):<11}"
                f"{f'{t2m - 273.15:+.0f}°C' if valid(t2m) else 'n/a':<7}"
                f"{wind('WindSpeed_AGL-10m', 'WindDir_AGL-10m'):<11}"
                f"{f'{gu * MS_TO_KT:.0f}kt' if valid(gu) else 'n/a':<7}"
                f"{f'{cc:.0f}%' if valid(cc) else 'n/a'}")
    return lines


# ---------------------------------------------------------------------------
# Block 2 (Nebelsicht) und Block 3 (TAF/METAR) — unveraendert zu v3
# ---------------------------------------------------------------------------

async def fog_block(client, tmpdir: Path, waypoints) -> list[str]:
    date, run = latest_expected_run(6)
    base = None
    for _ in range(2):
        for cand in RDPS_WEONG_BASES:
            url = (f"{cand}/{run}/003/{date}T{run}Z_MSC_RDPS-WEonG_"
                   f"LiquidFogVisibility_Sfc_RLatLon0.09_PT003H.grib2")
            try:
                if (await client.head(url, timeout=30.0)).status_code == 200:
                    base = cand
                    break
            except httpx.HTTPError:
                continue
        if base:
            break
        date, run = previous_run(date, run)
    if not base:
        return ["\n\nBLOCK 2 — RDPS-WEonG Nebelsicht: Quelle nicht "
                "erreichbar (Alpha-Datamart/Dateimuster pruefen)."]

    points = [(w.lat, w.lon) for w in waypoints]
    fog = {}
    for fh in FOG_HOURS:
        url = (f"{base}/{run}/{fh:03d}/{date}T{run}Z_MSC_RDPS-WEonG_"
               f"LiquidFogVisibility_Sfc_RLatLon0.09_PT{fh:03d}H.grib2")
        p = await fetch(client, url, tmpdir / f"fog{fh}.grib2")
        fog[fh] = extract_points(p, points) if p else None
        if p:
            p.unlink(missing_ok=True)

    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"\n\nBLOCK 2 — RDPS-WEonG Nebelsicht 10 km, Lauf {date} {run}Z",
             "=" * 70,
             "Werte km; '>=10' = keine Nebeleinschraenkung. "
             "Kritisch < 5 km, hart < 1.6 km."]
    lines.append("\nVT (UTC)   " + "".join(f"{w.icao:>7}" for w in waypoints))
    for fh in FOG_HOURS:
        vals = fog.get(fh)
        vt = run_dt + timedelta(hours=fh)
        row = f"{vt:%d. %H}Z    "
        for wi in range(len(waypoints)):
            v = vals[wi] if vals is not None else None
            row += (f"{'n/a':>7}" if not valid(v)
                    else (f"{'>=10':>7}" if v / 1000 >= 10
                          else f"{v / 1000:>6.1f}k"))
        lines.append(row)
    return lines


# ---------------------------------------------------------------------------
# Stufe 2: Persistenz-Abgleich — aktuelle METARs korrigieren die Kurzfrist.
# Regel: Ist die Beobachtung JETZT schlechter als das Modell, wird die
# Ampel fuer die naechsten Stunden angehoben (nur verschaerfend, nie
# entwarnend): volle Uebernahme bis +6 h, eine Stufe abgeschwaecht bis
# +12 h ab Beobachtungszeit. Nur METARs juenger als 90 min zaehlen.
# ---------------------------------------------------------------------------

METAR_TIME_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
OBS_MAX_AGE_MIN = 90
OBS_FULL_H, OBS_TAPER_H = 6.0, 12.0
STEP_DOWN = {"NOGO": "WARN", "WARN": "OK", "OK": "OK"}


def parse_metar_obs(raw: str, now: datetime) -> dict | None:
    """Beobachtungszeit, Nebel- und Ceiling-Kategorie aus rohem METAR."""
    m = METAR_TIME_RE.search(raw)
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    obs_time = None
    for k in (0, 1):                       # dieser oder vorheriger Monat
        y = now.year - (1 if now.month == 1 and k else 0)
        mo = now.month - k if now.month - k >= 1 else 12
        try:
            cand = now.replace(year=y, month=mo, day=day, hour=hh,
                               minute=mm, second=0, microsecond=0)
        except ValueError:
            continue
        if -1.5 * 3600 <= (now - cand).total_seconds() <= 32 * 86400:
            obs_time = cand
            break
    if obs_time is None:
        return None

    vis_km = None
    v = re.search(r"\b(?:M)?(\d+)?(?:\s+)?(\d)/(\d)SM\b", raw)
    if v:
        vis_km = ((int(v.group(1)) if v.group(1) else 0)
                  + int(v.group(2)) / int(v.group(3))) * 1.609
    else:
        v = re.search(r"\b(\d+)SM\b", raw)
        if v:
            vis_km = int(v.group(1)) * 1.609
    fog = re.search(r"\b(?:\+|-)?(?:FZ)?FG\b", raw) is not None
    mist = re.search(r"\bBR\b", raw) is not None
    if fog or (vis_km is not None and vis_km < 1.6):
        fog_cat = "NOGO"
    elif mist or (vis_km is not None and vis_km < 5.0):
        fog_cat = "WARN"
    else:
        fog_cat = "OK"

    layers = [int(h) * 100 for _t, h in
              re.findall(r"\b(VV|BKN|OVC)(\d{3})\b", raw)]
    cig_cat = cls_cig(min(layers)) if layers else "OK"
    return {"time": obs_time, "fog_cat": fog_cat, "cig_cat": cig_cat,
            "raw": raw.strip()}


async def fetch_awc(client, kind: str, ids: str) -> str:
    try:
        r = await client.get(f"{AWC_API}/{kind}",
                             params={"ids": ids, "format": "raw"},
                             headers={"User-Agent":
                                      "caps-route-briefing/1.0"},
                             timeout=45.0)
        return r.text.strip() if r.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


async def fetch_current_obs(client, waypoints, now: datetime
                            ) -> tuple[dict, str, str]:
    """(obs je ICAO fuer den Persistenz-Abgleich, METAR-Rohtext, TAF-Rohtext)."""
    ids = ",".join(w.icao for w in waypoints)
    metar_raw = await fetch_awc(client, "metar", ids)
    taf_raw = await fetch_awc(client, "taf", ids)
    obs: dict[str, dict] = {}
    icaos = {w.icao for w in waypoints}
    for line in metar_raw.splitlines():
        line = line.strip()
        for icao in icaos:
            if line.startswith((icao, f"METAR {icao}", f"SPECI {icao}")):
                parsed = parse_metar_obs(line, now)
                if parsed:
                    age_min = (now - parsed["time"]).total_seconds() / 60
                    if age_min <= OBS_MAX_AGE_MIN:
                        cur = obs.get(icao)
                        if cur is None or parsed["time"] > cur["time"]:
                            obs[icao] = parsed
                break
    return obs, metar_raw, taf_raw


def persistence_floor(entry: dict | None, vt: datetime
                      ) -> tuple[str | None, str | None]:
    """(Nebel-Mindestklasse, Ceiling-Mindestklasse) fuer Vorhersagezeit vt."""
    if entry is None:
        return None, None
    dt_h = (vt - entry["time"]).total_seconds() / 3600
    if dt_h < 0 or dt_h > OBS_TAPER_H:
        return None, None
    if dt_h <= OBS_FULL_H:
        return entry["fog_cat"], entry["cig_cat"]
    return STEP_DOWN[entry["fog_cat"]], STEP_DOWN[entry["cig_cat"]]


def metar_taf_lines(metar_raw: str, taf_raw: str) -> list[str]:
    lines = ["\n\nBLOCK 3 — Amtliche Meldungen (aviationweather.gov)",
             "=" * 70]
    for kind, body in (("METAR", metar_raw), ("TAF", taf_raw)):
        lines += [f"\n--- {kind} ---",
                  body or f"({kind} derzeit nicht abrufbar)"]
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(waypoints) -> str:
    out = [f"NWP-ROUTENBRIEFING — erzeugt "
           f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
           "Plaetze: " + ", ".join(
               w.icao + ("*" if w.role == "ALT" else "") for w in waypoints)
           + "   (* = Ausweichplatz)",
           "HINWEIS: Automatisierte Auswertung als Planungshilfe — ersetzt "
           "kein amtliches Briefing und keine PIC-Entscheidung.", ""]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        now = datetime.now(timezone.utc)
        obs, metar_raw, taf_raw = await fetch_current_obs(
            client, waypoints, now)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            data, date, run = await load_caps(client, tmpdir, waypoints)
            run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
                tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - run_dt).total_seconds() / 3600
            out.insert(1, f"VORHERSAGE: CAPS-Modelllauf {date} {run}:00 UTC "
                          f"(Alter {age_h:.1f} h) — gueltig "
                          f"{run_dt + timedelta(hours=CAPS_HOURS[0]):%d.%m. %H}Z "
                          f"bis {run_dt + timedelta(hours=CAPS_HOURS[-1]):%d.%m. %H}Z")
            out += dashboard_block(data, date, run, waypoints, obs=obs)
            out += caps_block(data, date, run, waypoints)
            out += await fog_block(client, tmpdir, waypoints)
        out += metar_taf_lines(metar_raw, taf_raw)
    out.append("\nLegende Block 1: W700~FL100, W850~5000ft; Sp = Taupunkt-"
               "Spread in °C (Zahlenwert identisch zu K).")
    return "\n".join(out)


if __name__ == "__main__":
    wanted = {a.upper() for a in sys.argv[1:]}
    wps = [w for w in ROUTE if w.icao in wanted] or ROUTE
    print(asyncio.run(main(wps)))
