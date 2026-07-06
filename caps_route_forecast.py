"""
caps_route_forecast.py
======================

48-h-Punktvorhersagen des Canadian Arctic Prediction System (CAPS,
Datamart-Pfad: HRDPS-North, ~3 km) entlang einer Flugroute.

Datenquelle (MSC Datamart, keine Authentifizierung):
    https://dd.weather.gc.ca/today/model_hrdps/north/3km/{HH}/{hhh}/

Abhängigkeiten:
    pip install httpx xarray cfgrib numpy
    (cfgrib benötigt die ecCodes-Bibliothek: apt install libeccodes0
     bzw. brew install eccodes)

Nutzung standalone:
    python caps_route_forecast.py                 # gesamte NWP-Route
    python caps_route_forecast.py CYRB CYCB       # nur einzelne Wegpunkte

Nutzung als MCP-Tool: `get_caps_route_forecast()` importieren und im
FastMCP-Server als Tool registrieren (Beispiel am Dateiende).
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

DATAMART_BASE = "https://dd.weather.gc.ca/today/model_hrdps/north/3km"

# ---------------------------------------------------------------------------
# Route: Nordwestpassage, Iqaluit -> Nome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Waypoint:
    icao: str
    name: str
    lat: float
    lon: float

ROUTE: list[Waypoint] = [
    Waypoint("CYFB", "Iqaluit",       63.756, -68.556),
    Waypoint("CYIO", "Pond Inlet",    72.683, -77.967),
    Waypoint("CYRB", "Resolute Bay",  74.717, -94.969),
    Waypoint("CYHK", "Gjoa Haven",    68.636, -95.850),
    Waypoint("CYCB", "Cambridge Bay", 69.108, -105.138),
    Waypoint("PABR", "Utqiagvik/Point Barrow", 71.285, -156.766),
    Waypoint("PAOM", "Nome",          64.512, -165.445),
]

# ---------------------------------------------------------------------------
# Variablen: (Datamart-VAR, Level-String im Dateinamen, Kurzbeschreibung)
# Druckflächen ~ Reiseflughöhen DA62: 700 hPa ≈ FL100, 850 hPa ≈ 5000 ft
# ---------------------------------------------------------------------------

VARIABLES: list[tuple[str, str, str]] = [
    ("UGRD", "ISBL_0700", "u-Wind 700 hPa (~FL100)"),
    ("VGRD", "ISBL_0700", "v-Wind 700 hPa (~FL100)"),
    ("UGRD", "ISBL_0850", "u-Wind 850 hPa (~5000 ft)"),
    ("VGRD", "ISBL_0850", "v-Wind 850 hPa (~5000 ft)"),
    ("TMP",  "ISBL_0700", "Temperatur 700 hPa"),
    ("TMP",  "ISBL_0850", "Temperatur 850 hPa"),
    ("TMP",  "AGL-2m",    "Temperatur 2 m"),
    ("GUST", "AGL-10m",   "Böen 10 m"),
    ("WIND", "AGL-10m",   "Wind 10 m"),
    ("WDIR", "AGL-10m",   "Windrichtung 10 m"),
]

# WEonG-Diagnostik (eigener Dateinamens-Stamm HRDPS-North-WEonG)
WEONG_VARIABLES: list[tuple[str, str, str]] = [
    ("VISIFG", "Sfc", "Sichtweite inkl. Nebel"),
]

FORECAST_HOURS = range(1, 49, 3)   # alle 3 h reicht fürs Briefing; 1..48 möglich


# ---------------------------------------------------------------------------
# Lauf-Erkennung und Download
# ---------------------------------------------------------------------------

def latest_expected_run(now: datetime | None = None) -> tuple[str, str]:
    """Jüngster wahrscheinlich vollständiger Lauf (00Z/12Z, ~5 h Latenz).

    Returns (YYYYMMDD, HH). Fällt der Kandidat aus, eine Stufe zurückgehen.
    """
    now = now or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=5)
    run_hour = "12" if candidate.hour >= 12 else "00"
    return candidate.strftime("%Y%m%d"), run_hour


def grib_url(date: str, run: str, var: str, level: str,
             fhour: int, weong: bool = False) -> str:
    stem = "HRDPS-North-WEonG" if weong else "HRDPS-North"
    fname = (f"{date}T{run}Z_MSC_{stem}_{var}_{level}"
             f"_RLatLon0.03_PT{fhour:03d}H.grib2")
    return f"{DATAMART_BASE}/{run}/{fhour:03d}/{fname}"


async def fetch(client: httpx.AsyncClient, url: str, dest: Path) -> Path | None:
    try:
        r = await client.get(url, timeout=60.0)
        if r.status_code != 200:
            return None
        dest.write_bytes(r.content)
        return dest
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# Punkt-Extraktion im rotierten Lat-Lon-Gitter
# ---------------------------------------------------------------------------

def nearest_index(ds: xr.Dataset, lat: float, lon: float) -> tuple[int, int]:
    """Nächstgelegener Gitterpunkt über 2D-Koordinatenfelder.

    cfgrib liefert für rotierte Gitter 'latitude'/'longitude' als 2D-Arrays
    in echten geographischen Koordinaten -> einfache Distanzminimierung.
    """
    lat2d = ds["latitude"].values
    lon2d = ds["longitude"].values
    lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
    # Näherung ausreichend bei 3-km-Gitter: gewichtete Grad-Distanz
    d2 = (lat2d - lat) ** 2 + ((lon2d - lon) * math.cos(math.radians(lat))) ** 2
    j, i = np.unravel_index(np.argmin(d2), d2.shape)
    return int(j), int(i)


def extract_point(grib_path: Path, lat: float, lon: float) -> float | None:
    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        j, i = nearest_index(ds, lat, lon)
        var = next(iter(ds.data_vars))
        val = float(ds[var].values[..., j, i].squeeze())
        ds.close()
        return val
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Aufbereitung
# ---------------------------------------------------------------------------

def wind_from_uv(u: float | None, v: float | None) -> tuple[float, float] | None:
    """(Richtung °true, Geschwindigkeit kt) aus u/v in m/s."""
    if u is None or v is None:
        return None
    speed_kt = math.hypot(u, v) * 1.9438
    direction = (math.degrees(math.atan2(-u, -v))) % 360
    return direction, speed_kt


async def get_caps_route_forecast(
    waypoints: list[Waypoint] | None = None,
    hours: range = FORECAST_HOURS,
) -> str:
    """48-h-CAPS-Briefing für alle Wegpunkte als formatierter Text."""
    waypoints = waypoints or ROUTE
    date, run = latest_expected_run()
    all_vars = ([(v, l, d, False) for v, l, d in VARIABLES]
                + [(v, l, d, True) for v, l, d in WEONG_VARIABLES])

    results: dict[int, dict[str, float | None]] = {}

    async with httpx.AsyncClient() as client:
        # Lauf-Verfügbarkeit prüfen, sonst 12 h zurück
        probe = grib_url(date, run, "TMP", "AGL-2m", hours.start)
        if (await client.head(probe, timeout=30.0)).status_code != 200:
            fallback = (datetime.strptime(date + run, "%Y%m%d%H")
                        .replace(tzinfo=timezone.utc) - timedelta(hours=12))
            date, run = fallback.strftime("%Y%m%d"), f"{fallback.hour:02d}"

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for fh in hours:
                async def one(v, l, d, weong, fh=fh):
                    url = grib_url(date, run, v, l, fh, weong)
                    path = await fetch(client, url,
                                       tmpdir / f"{v}_{l}_{fh}.grib2")
                    return (v, l), path
                downloads = await asyncio.gather(
                    *(one(v, l, d, w) for v, l, d, w in all_vars))
                results[fh] = {}
                for (v, l), path in downloads:
                    results[fh][f"{v}_{l}"] = path  # Pfade erst sammeln

    # Extraktion pro Wegpunkt (CPU-gebunden, nach den Downloads)
    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"CAPS/HRDPS-North 3 km — Lauf {date} {run}Z "
             f"(Gitterpunkt-Vorhersage, 48 h)\n"]

    for wp in waypoints:
        lines.append(f"\n=== {wp.icao} {wp.name} "
                     f"({wp.lat:.3f}, {wp.lon:.3f}) ===")
        lines.append(f"{'VT (UTC)':<14}{'700hPa Wind':<16}{'850hPa Wind':<16}"
                     f"{'T700':<7}{'T850':<7}{'T2m':<7}{'Böen':<7}{'Sicht'}")
        for fh in hours:
            vals = {k: (extract_point(p, wp.lat, wp.lon)
                        if isinstance(p, Path) else None)
                    for k, p in results[fh].items()}
            w700 = wind_from_uv(vals.get("UGRD_ISBL_0700"),
                                vals.get("VGRD_ISBL_0700"))
            w850 = wind_from_uv(vals.get("UGRD_ISBL_0850"),
                                vals.get("VGRD_ISBL_0850"))
            def fmt_wind(w): return (f"{w[0]:03.0f}/{w[1]:.0f}kt"
                                     if w else "n/a")
            def fmt_t(k):
                v = vals.get(k)
                return f"{v - 273.15:+.0f}°C" if v is not None else "n/a"
            gust = vals.get("GUST_AGL-10m")
            vis = vals.get("VISIFG_Sfc")
            vt = run_dt + timedelta(hours=fh)
            lines.append(
                f"{vt:%d. %H:%M}Z    {fmt_wind(w700):<16}{fmt_wind(w850):<16}"
                f"{fmt_t('TMP_ISBL_0700'):<7}{fmt_t('TMP_ISBL_0850'):<7}"
                f"{fmt_t('TMP_AGL-2m'):<7}"
                f"{f'{gust * 1.9438:.0f}kt' if gust else 'n/a':<7}"
                f"{f'{vis / 1000:.1f}km' if vis else 'n/a'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standalone-Aufruf & MCP-Integration
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    wanted = [w for w in ROUTE if w.icao in sys.argv[1:]] or ROUTE
    print(asyncio.run(get_caps_route_forecast(wanted)))


# --- Integration in den bestehenden FastMCP-Server (Windy) ------------------
#
# from caps_route_forecast import get_caps_route_forecast, ROUTE, Waypoint
#
# @mcp.tool()
# async def caps_briefing(icao_codes: str = "") -> str:
#     """48h-Punktvorhersage des CAPS (3km, Arktis) fuer Wegpunkte der
#     Nordwestpassagen-Route. icao_codes: kommagetrennt, leer = ganze Route."""
#     codes = {c.strip().upper() for c in icao_codes.split(",") if c.strip()}
#     wps = [w for w in ROUTE if w.icao in codes] or ROUTE
#     return await get_caps_route_forecast(wps)
