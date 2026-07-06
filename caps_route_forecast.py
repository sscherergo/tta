"""
caps_route_forecast.py — v3
===========================

Kombiniertes Wetterbriefing fuer die Nordwestpassagen-Route (DA62):

  Block 1: CAPS 3 km        — Wind/Temp/Spread/Bedeckung, 48 h (verifiziert)
  Block 2: RDPS-WEonG 10 km — Nebelsicht (LiquidFogVisibility), bis 84 h
  Block 3: TAF/METAR        — amtliche Meldungen via aviationweather.gov

Route + Ausweichplaetze im ±100-nm-Korridor. Punkte ausserhalb des
CAPS-Gebiets (z. B. Nome) werden erkannt und als 'ausserhalb' markiert.

Abhängigkeiten: pip install httpx xarray cfgrib numpy   (+ ecCodes)
Nutzung:        python caps_route_forecast.py [ICAO ...]
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
RDPS_WEONG_BASES = [   # Kandidaten, werden der Reihe nach probiert
    "https://dd.alpha.weather.gc.ca/model_rdps/10km",
    "https://dd.alpha.weather.gc.ca/model_gem_regional/10km",
    "https://dd.weather.gc.ca/today/model_rdps/10km",
]
AWC_API = "https://aviationweather.gov/api/data"

MS_TO_KT = 1.9438
MAX_GRID_DIST_DEG = 0.10   # ~10 km: Punkt weiter weg -> ausserhalb Domain

# ---------------------------------------------------------------------------
# Route und Ausweichplaetze (±100 nm Korridor)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Waypoint:
    icao: str
    name: str
    lat: float
    lon: float
    role: str = "RTE"        # RTE = Routenplatz, ALT = Ausweichplatz

ROUTE: list[Waypoint] = [
    # --- Hauptroute ---
    Waypoint("CYFB", "Iqaluit",        63.756,  -68.556),
    Waypoint("CYIO", "Pond Inlet",     72.683,  -77.967),
    Waypoint("CYRB", "Resolute Bay",   74.717,  -94.969),
    Waypoint("CYHK", "Gjoa Haven",     68.636,  -95.850),
    Waypoint("CYCB", "Cambridge Bay",  69.108, -105.138),
    Waypoint("PABR", "Utqiagvik",      71.285, -156.766),
    Waypoint("PAOM", "Nome",           64.512, -165.445),
    # --- Ausweichplaetze, Leg CYFB-CYIO ---
    Waypoint("CYCY", "Clyde River",    70.486,  -68.517, "ALT"),
    # --- Leg CYIO-CYRB ---
    Waypoint("CYAB", "Arctic Bay",     73.006,  -85.047, "ALT"),
    # --- Legs CYRB-CYHK-CYCB ---
    Waypoint("CYYH", "Taloyoak",       69.547,  -93.577, "ALT"),
    Waypoint("CYBB", "Kugaaruk",       68.534,  -89.808, "ALT"),
    # --- Leg CYCB-PABR ---
    Waypoint("CYCO", "Kugluktuk",      67.817, -115.144, "ALT"),
    Waypoint("CYHI", "Ulukhaktok",     70.763, -117.806, "ALT"),
    Waypoint("CYPC", "Paulatuk",       69.361, -124.075, "ALT"),
    Waypoint("CYUB", "Tuktoyaktuk",    69.433, -133.026, "ALT"),
    Waypoint("CYEV", "Inuvik",         68.304, -133.483, "ALT"),
    Waypoint("PASC", "Deadhorse",      70.195, -148.465, "ALT"),
    # --- Leg PABR-PAOM ---
    Waypoint("PAWI", "Wainwright",     70.638, -159.995, "ALT"),
    Waypoint("PAPO", "Point Hope",     68.349, -166.799, "ALT"),
    Waypoint("PAOT", "Kotzebue",       66.885, -162.599, "ALT"),
]

# CAPS-Variablen (verifiziert 2026-07-06)
CAPS_VARIABLES: list[tuple[str, str]] = [
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
]

CAPS_HOURS = range(3, 49, 3)
FOG_HOURS = list(range(3, 49, 3)) + list(range(54, 85, 6))


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Lauf, Download, Punkt-Extraktion
# ---------------------------------------------------------------------------

def latest_expected_run(latency_h: int, now: datetime | None = None
                        ) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    c = now - timedelta(hours=latency_h)
    return c.strftime("%Y%m%d"), "12" if c.hour >= 12 else "00"


def previous_run(date: str, run: str) -> tuple[str, str]:
    fb = (datetime.strptime(date + run, "%Y%m%d%H")
          .replace(tzinfo=timezone.utc) - timedelta(hours=12))
    return fb.strftime("%Y%m%d"), f"{fb.hour:02d}"


async def fetch(client: httpx.AsyncClient, url: str, dest: Path) -> Path | None:
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
    """Nearest-Gridpoint-Werte; None fuer Punkte ausserhalb der Domain."""
    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        lat2d = ds["latitude"].values
        lon2d = ds["longitude"].values
        lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
        field = ds[next(iter(ds.data_vars))].values.squeeze()
        out: list[float] = []
        for lat, lon in points:
            d2 = ((lat2d - lat) ** 2
                  + ((lon2d - lon) * math.cos(math.radians(lat))) ** 2)
            j, i = np.unravel_index(np.argmin(d2), d2.shape)
            if math.sqrt(d2[j, i]) > MAX_GRID_DIST_DEG:
                out.append(math.nan)          # ausserhalb der Modelldomain
            else:
                out.append(float(field[j, i]))
        ds.close()
        return out
    except Exception as exc:
        print(f"  [PARSE] {grib_path.name}: {exc}", file=sys.stderr)
        return None


def valid(v: float | None) -> bool:
    return v is not None and not math.isnan(v)


# ---------------------------------------------------------------------------
# Block 1: CAPS
# ---------------------------------------------------------------------------

async def caps_block(client: httpx.AsyncClient, tmpdir: Path,
                     waypoints: list[Waypoint]) -> list[str]:
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
            p = await fetch(client, url, tmpdir / f"c{var}_{lvl}_{fh}.grib2")
            return f"{var}_{lvl}", p
        res = await asyncio.gather(*(one(v, l) for v, l in CAPS_VARIABLES))
        data[fh] = {k: (extract_points(p, points) if p else None)
                    for k, p in res}
        for _, p in res:
            if p:
                p.unlink(missing_ok=True)

    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"BLOCK 1 — CAPS 3 km, Lauf {date} {run}Z (48 h)",
             "=" * 70]

    def g(fh, key, wi):
        vals = data[fh].get(key)
        return vals[wi] if vals is not None else None

    for wi, wp in enumerate(waypoints):
        # Domain-Check anhand der ersten Stunde
        t_probe = g(CAPS_HOURS[0], "AirTemp_AGL-2m", wi)
        tag = "" if wp.role == "RTE" else " [ALT]"
        lines.append(f"\n=== {wp.icao} {wp.name}{tag} ===")
        if t_probe is not None and math.isnan(t_probe):
            lines.append("    ausserhalb der CAPS-Domain -> siehe Block 2/3")
            continue
        lines.append(f"{'VT (UTC)':<13}{'W700':<11}{'W850':<11}"
                     f"{'T700/Sp':<11}{'T850/Sp':<11}{'T2m':<7}"
                     f"{'Wind10m':<11}{'Böen':<7}{'Bew.'}")
        for fh in CAPS_HOURS:
            def wind(sk, dk):
                s, d = g(fh, sk, wi), g(fh, dk, wi)
                return (f"{d:03.0f}/{s * MS_TO_KT:.0f}kt"
                        if valid(s) and valid(d) else "n/a")
            def tsp(tk, sk):
                t, sp = g(fh, tk, wi), g(fh, sk, wi)
                if not valid(t):
                    return "n/a"
                base = f"{t - 273.15:+.0f}"
                return f"{base}/{sp:.0f}K" if valid(sp) else base
            t2m = g(fh, "AirTemp_AGL-2m", wi)
            gu = g(fh, "WindGust_AGL-10m", wi)
            cc = g(fh, "TotalCloudCover_Sfc", wi)
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
# Block 2: RDPS-WEonG Nebelsicht
# ---------------------------------------------------------------------------

async def fog_block(client: httpx.AsyncClient, tmpdir: Path,
                    waypoints: list[Waypoint]) -> list[str]:
    date, run = latest_expected_run(6)
    base = None
    for attempt in range(2):                      # aktueller + vorheriger Lauf
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
        return ["BLOCK 2 — RDPS-WEonG Nebelsicht: Quelle nicht erreichbar "
                "(Alpha-Datamart prüfen; Dateimuster ggf. geändert)."]

    points = [(w.lat, w.lon) for w in waypoints]
    fog: dict[int, list[float] | None] = {}
    for fh in FOG_HOURS:
        url = (f"{base}/{run}/{fh:03d}/{date}T{run}Z_MSC_RDPS-WEonG_"
               f"LiquidFogVisibility_Sfc_RLatLon0.09_PT{fh:03d}H.grib2")
        p = await fetch(client, url, tmpdir / f"fog_{fh}.grib2")
        fog[fh] = extract_points(p, points) if p else None
        if p:
            p.unlink(missing_ok=True)

    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"\n\nBLOCK 2 — RDPS-WEonG Nebelsicht 10 km, "
             f"Lauf {date} {run}Z (84 h)", "=" * 70,
             "Werte in km; '>=10' = keine relevante Nebeleinschraenkung.",
             "Kritisch fuer VFR/Anflug: < 5 km, hart: < 1.6 km (1 SM)."]

    header = "VT (UTC)   " + "".join(f"{w.icao:>7}" for w in waypoints)
    lines.append("\n" + header)
    for fh in FOG_HOURS:
        vals = fog.get(fh)
        vt = run_dt + timedelta(hours=fh)
        row = f"{vt:%d. %H}Z    "
        for wi in range(len(waypoints)):
            v = vals[wi] if vals is not None else None
            if not valid(v):
                row += f"{'n/a':>7}"
            else:
                km = v / 1000.0
                row += f"{'>=10':>7}" if km >= 10 else f"{km:>6.1f}k"
        lines.append(row)
    return lines


# ---------------------------------------------------------------------------
# Block 3: TAF / METAR (aviationweather.gov)
# ---------------------------------------------------------------------------

async def taf_metar_block(client: httpx.AsyncClient,
                          waypoints: list[Waypoint]) -> list[str]:
    ids = ",".join(w.icao for w in waypoints)
    lines = ["\n\nBLOCK 3 — Amtliche Meldungen (aviationweather.gov)",
             "=" * 70]
    for kind in ("metar", "taf"):
        try:
            r = await client.get(
                f"{AWC_API}/{kind}",
                params={"ids": ids, "format": "raw"},
                headers={"User-Agent": "caps-route-briefing/1.0"},
                timeout=45.0)
            body = r.text.strip() if r.status_code == 200 else ""
        except httpx.HTTPError:
            body = ""
        lines.append(f"\n--- {kind.upper()} ---")
        lines.append(body if body else
                     f"({kind.upper()} derzeit nicht abrufbar)")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(waypoints: list[Waypoint]) -> str:
    out = [f"NWP-ROUTENBRIEFING — erzeugt "
           f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
           f"Plaetze: " + ", ".join(
               w.icao + ("*" if w.role == "ALT" else "")
               for w in waypoints) + "   (* = Ausweichplatz)", ""]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            out += await caps_block(client, tmpdir, waypoints)
            out += await fog_block(client, tmpdir, waypoints)
        out += await taf_metar_block(client, waypoints)
    out.append("\nLegende: W700~FL100, W850~5000ft; Sp = Taupunkt-Spread "
               "(Vereisung bei Sp<3K und T<0°C); Nebel-Setup: Spread<1.5K "
               "+ Wind<5kt + Bedeckung hoch.")
    return "\n".join(out)


if __name__ == "__main__":
    wanted_codes = {a.upper() for a in sys.argv[1:]}
    wps = [w for w in ROUTE if w.icao in wanted_codes] or ROUTE
    print(asyncio.run(main(wps)))
