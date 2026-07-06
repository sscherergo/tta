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
    "CYIO": None,
    "CYRB": 170.0,   # RWY 17T/35T
    "CYHK": None,
    "CYCB": 130.0,   # RWY 13T/31T
    "PABR": 80.0,    # RWY 07/25 mag + ~11E Var
    "PAOM": 110.0,   # RWY 10/28 mag + ~9E Var
    "CYCY": None, "CYAB": None, "CYYH": None, "CYBB": None,
    "CYCO": None, "CYHI": None, "CYPC": None, "CYUB": None,
    "CYEV": 60.0,    # RWY 06T/24T
    "PASC": None, "PAWI": None, "PAPO": None, "PAOT": None,
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
    ("WindGust",           "AGL-10m"),
    ("WindSpeed",          "AGL-10m"),
    ("WindDir",            "AGL-10m"),
    ("TotalCloudCover",    "Sfc"),
    ("Pressure",           "Sfc"),
] + [("RelativeHumidity", f"IsbL-{l}") for l in CEILING_LEVELS])

CAPS_HOURS = range(3, 49, 3)
DASH_HOURS = range(6, 49, 6)
FOG_HOURS = list(range(3, 49, 3)) + list(range(54, 85, 6))


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
    date, run = latest_expected_run(6)
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
    """Basis niedrigste Druckflaeche mit RH>=90%, barometrisch in ft AGL.
    Rueckgabe: ft AGL; 99999 = keine Basis unterhalb ~5000 ft."""
    p_sfc = gv(data, fh, "Pressure_Sfc", wi)          # Pa
    if not valid(p_sfc):
        return None
    for lvl in CEILING_LEVELS:                        # hoechster Druck zuerst
        p_lvl = float(lvl) * 100.0
        if p_lvl >= p_sfc:
            continue                                  # Level "unter Grund"
        rh = gv(data, fh, f"RelativeHumidity_IsbL-{lvl}", wi)
        if valid(rh) and rh >= 90.0:
            return 8000.0 * math.log(p_sfc / p_lvl) * 3.281
    return 99999.0


def min_spread(data, fh, wi) -> float | None:
    s7 = gv(data, fh, "DewPointDepression_IsbL-0700", wi)
    s8 = gv(data, fh, "DewPointDepression_IsbL-0850", wi)
    vals = [s for s in (s7, s8) if valid(s)]
    return min(vals) if vals else None


def cls_hw(v):  return "G" if v < 10 else ("O" if v <= 20 else "R")
def cls_xw(v):  return "G" if v <= 10 else ("O" if v <= 20 else "R")
def cls_cig(v): return "G" if v >= 5000 else ("O" if v >= 2000 else "R")
def cls_sp(v):  return "R" if v <= 1.5 else ("O" if v <= 3.0 else "G")

WORST = {"G": 0, "O": 1, "R": 2, "?": 1}
LETTER = {0: "G", 1: "O", 2: "R"}


def dashboard_block(data, date, run, waypoints) -> list[str]:
    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    coords = {w.icao: w for w in waypoints}
    lines = [
        "BLOCK 0 — MACHBARKEIT (personalisierte Bewertung, kein amtliches "
        "Produkt)", "=" * 70,
        "G=gruen O=orange R=rot ?=Daten fehlen  |  Gesamt = schlechteste "
        "Einzelwertung",
        "HW: Headwind 8000ft im Anflugkurs | XW: Crosswind aus Boeen "
        "(~ = Piste unbekannt, volle Boe) | CIG: Wolkenbasis ft AGL | "
        "SP: min. Spread 850/700", ""]

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

        lines.append(f"{'VT (UTC)':<12}{'HW kt':>9}{'XW kt':>9}"
                     f"{'CIG ft':>10}{'SP K':>7}   GESAMT")
        for fh in DASH_HOURS:
            hw = headwind_8000(data, fh, wi, course) \
                if course is not None else None
            xw, exact = crosswind_gust(data, fh, wi, rwy)
            cig = ceiling_ft(data, fh, wi)
            sp = min_spread(data, fh, wi)

            parts, worst = [], 0
            def one(v, cls, fmt, mark=""):
                nonlocal worst
                if not valid(v):
                    parts.append(f"{'?':>8}")
                    worst = max(worst, WORST["?"])
                else:
                    c = cls(v)
                    worst = max(worst, WORST[c])
                    parts.append(f"{fmt(v)}{mark}{c:>2}")
            one(hw, cls_hw, lambda v: f"{v:+6.0f}") if course is not None \
                else (parts.append(f"{'—':>8}"))
            one(xw, cls_xw, lambda v: f"{v:6.0f}", "" if exact else "~")
            one(cig, cls_cig,
                lambda v: ("  >5000" if v >= 99999 else f"{v:7.0f}"))
            one(sp, cls_sp, lambda v: f"{v:5.1f}")
            vt = run_dt + timedelta(hours=fh)
            lines.append(f"{vt:%d. %H}Z     " + " ".join(parts)
                         + f"     [{LETTER[worst]}]")
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


async def taf_metar_block(client, waypoints) -> list[str]:
    ids = ",".join(w.icao for w in waypoints)
    lines = ["\n\nBLOCK 3 — Amtliche Meldungen (aviationweather.gov)",
             "=" * 70]
    for kind in ("metar", "taf"):
        try:
            r = await client.get(f"{AWC_API}/{kind}",
                                 params={"ids": ids, "format": "raw"},
                                 headers={"User-Agent":
                                          "caps-route-briefing/1.0"},
                                 timeout=45.0)
            body = r.text.strip() if r.status_code == 200 else ""
        except httpx.HTTPError:
            body = ""
        lines += [f"\n--- {kind.upper()} ---",
                  body or f"({kind.upper()} derzeit nicht abrufbar)"]
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
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            data, date, run = await load_caps(client, tmpdir, waypoints)
            out += dashboard_block(data, date, run, waypoints)
            out += caps_block(data, date, run, waypoints)
            out += await fog_block(client, tmpdir, waypoints)
        out += await taf_metar_block(client, waypoints)
    out.append("\nLegende Block 1: W700~FL100, W850~5000ft; Sp = Spread "
               "(Vereisung bei Sp<3K und T<0°C).")
    return "\n".join(out)


if __name__ == "__main__":
    wanted = {a.upper() for a in sys.argv[1:]}
    wps = [w for w in ROUTE if w.icao in wanted] or ROUTE
    print(asyncio.run(main(wps)))
