"""
caps_route_forecast.py
======================

48-h-Punktvorhersagen des Canadian Arctic Prediction System (CAPS, ~3 km)
entlang einer Flugroute. Pfade und Dateinamen verifiziert am 2026-07-06
gegen den MSC Datamart.

Datenquelle (keine Authentifizierung):
    https://dd.weather.gc.ca/today/model_caps/3km/{HH}/{hhh}/
Dateischema:
    {YYYYMMDD}T{HH}Z_MSC_CAPS_{Variable}_{Level}_RLatLon0.03_PT{hhh}H.grib2

Abhängigkeiten:
    pip install httpx xarray cfgrib numpy
    (cfgrib benötigt ecCodes: apt install libeccodes0 / brew install eccodes)

Nutzung:
    python caps_route_forecast.py                 # gesamte NWP-Route
    python caps_route_forecast.py CYRB CYCB       # nur einzelne Wegpunkte
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

DATAMART_BASE = "https://dd.weather.gc.ca/today/model_caps/3km"

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
# Variablen: (Variable, Level) exakt wie im Datamart-Dateinamen.
# WindSpeed/WindDir statt U/V: vermeidet die Gitterrotations-Korrektur.
# DewPointDepression (Spread) auf Druckflaechen = Wolken-/Vereisungsindikator.
# 700 hPa ~ FL100, 850 hPa ~ 5000 ft.
# ---------------------------------------------------------------------------

VARIABLES: list[tuple[str, str]] = [
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

FORECAST_HOURS = range(3, 49, 3)   # alle 3 h; fuer stuendlich: range(1, 49)

MS_TO_KT = 1.9438


# ---------------------------------------------------------------------------
# Lauf-Erkennung, URL-Bau, Download
# ---------------------------------------------------------------------------

def latest_expected_run(now: datetime | None = None) -> tuple[str, str]:
    """Juengster wahrscheinlich vollstaendiger Lauf (00Z/12Z, ~6 h Latenz)."""
    now = now or datetime.now(timezone.utc)
    candidate = now - timedelta(hours=6)
    run_hour = "12" if candidate.hour >= 12 else "00"
    return candidate.strftime("%Y%m%d"), run_hour


def grib_url(date: str, run: str, var: str, level: str, fhour: int) -> str:
    fname = (f"{date}T{run}Z_MSC_CAPS_{var}_{level}"
             f"_RLatLon0.03_PT{fhour:03d}H.grib2")
    return f"{DATAMART_BASE}/{run}/{fhour:03d}/{fname}"


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


# ---------------------------------------------------------------------------
# Punkt-Extraktion im rotierten Lat-Lon-Gitter
# ---------------------------------------------------------------------------

def extract_points(grib_path: Path,
                   points: list[tuple[float, float]]) -> list[float] | None:
    """Werte am naechstgelegenen Gitterpunkt fuer mehrere Koordinaten.

    cfgrib liefert fuer rotierte Gitter 'latitude'/'longitude' als
    2D-Felder in echten geographischen Koordinaten.
    """
    try:
        ds = xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        lat2d = ds["latitude"].values
        lon2d = ds["longitude"].values
        lon2d = np.where(lon2d > 180, lon2d - 360, lon2d)
        var = next(iter(ds.data_vars))
        field = ds[var].values.squeeze()
        out = []
        for lat, lon in points:
            d2 = ((lat2d - lat) ** 2
                  + ((lon2d - lon) * math.cos(math.radians(lat))) ** 2)
            j, i = np.unravel_index(np.argmin(d2), d2.shape)
            out.append(float(field[j, i]))
        ds.close()
        return out
    except Exception as exc:
        print(f"  [PARSE] {grib_path.name}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------

async def get_caps_route_forecast(
    waypoints: list[Waypoint] | None = None,
    hours: range = FORECAST_HOURS,
) -> str:
    waypoints = waypoints or ROUTE
    date, run = latest_expected_run()
    points = [(w.lat, w.lon) for w in waypoints]

    # {fhour: {"Var_Level": [wert_pro_wegpunkt]}}
    data: dict[int, dict[str, list[float] | None]] = {}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Lauf verfuegbar? Sonst 12 h zurueckfallen.
        probe = grib_url(date, run, "AirTemp", "AGL-2m", hours[-1])
        if (await client.head(probe, timeout=30.0)).status_code != 200:
            fb = (datetime.strptime(date + run, "%Y%m%d%H")
                  .replace(tzinfo=timezone.utc) - timedelta(hours=12))
            date, run = fb.strftime("%Y%m%d"), f"{fb.hour:02d}"
            print(f"Fallback auf Lauf {date} {run}Z", file=sys.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for fh in hours:
                async def one(var, lvl, fh=fh):
                    url = grib_url(date, run, var, lvl, fh)
                    p = await fetch(client, url,
                                    tmpdir / f"{var}_{lvl}_{fh:03d}.grib2")
                    return f"{var}_{lvl}", p
                results = await asyncio.gather(
                    *(one(v, l) for v, l in VARIABLES))
                data[fh] = {key: (extract_points(p, points) if p else None)
                            for key, p in results}
                for key, p in results:      # Platz sofort freigeben
                    if p:
                        p.unlink(missing_ok=True)

    run_dt = datetime.strptime(date + run, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    lines = [f"CAPS 3 km — Lauf {date} {run}Z — "
             f"Gitterpunkt-Vorhersage, {hours.start}-{hours[-1]} h"]

    def g(fh: int, key: str, wp_idx: int) -> float | None:
        vals = data[fh].get(key)
        return vals[wp_idx] if vals is not None else None

    for wi, wp in enumerate(waypoints):
        lines.append(f"\n=== {wp.icao} {wp.name} "
                     f"({wp.lat:.3f}, {wp.lon:.3f}) ===")
        lines.append(f"{'VT (UTC)':<13}{'W700':<11}{'W850':<11}"
                     f"{'T700/Sp':<11}{'T850/Sp':<11}{'T2m':<7}"
                     f"{'Wind10m':<11}{'Böen':<7}{'Bew.'}")
        for fh in hours:
            def wind(spd_key, dir_key):
                s, d = g(fh, spd_key, wi), g(fh, dir_key, wi)
                if s is None or d is None:
                    return "n/a"
                return f"{d:03.0f}/{s * MS_TO_KT:.0f}kt"
            def temp_spread(t_key, sp_key):
                t, sp = g(fh, t_key, wi), g(fh, sp_key, wi)
                if t is None:
                    return "n/a"
                ts = f"{t - 273.15:+.0f}"
                return f"{ts}/{sp:.0f}K" if sp is not None else ts
            t2m = g(fh, "AirTemp_AGL-2m", wi)
            gust = g(fh, "WindGust_AGL-10m", wi)
            cc = g(fh, "TotalCloudCover_Sfc", wi)
            vt = run_dt + timedelta(hours=fh)
            lines.append(
                f"{vt:%d. %H}Z      "
                f"{wind('WindSpeed_IsbL-0700', 'WindDir_IsbL-0700'):<11}"
                f"{wind('WindSpeed_IsbL-0850', 'WindDir_IsbL-0850'):<11}"
                f"{temp_spread('AirTemp_IsbL-0700',
                               'DewPointDepression_IsbL-0700'):<11}"
                f"{temp_spread('AirTemp_IsbL-0850',
                               'DewPointDepression_IsbL-0850'):<11}"
                f"{f'{t2m - 273.15:+.0f}°C' if t2m is not None else 'n/a':<7}"
                f"{wind('WindSpeed_AGL-10m', 'WindDir_AGL-10m'):<11}"
                f"{f'{gust * MS_TO_KT:.0f}kt' if gust is not None else 'n/a':<7}"
                f"{f'{cc:.0f}%' if cc is not None else 'n/a'}")

    lines.append("\nLegende: W700~FL100, W850~5000ft; Sp = Taupunkt-Spread"
                 " (Vereisungsrisiko bei Sp<3K und T<0°C); Bew. = Bedeckung.")
    return "\n".join(lines)


if __name__ == "__main__":
    wanted = [w for w in ROUTE if w.icao in
              {a.upper() for a in sys.argv[1:]}] or ROUTE
    print(asyncio.run(get_caps_route_forecast(wanted)))
