#!/usr/bin/env python3
"""Greenland-Leg-Briefing BIRK -> BGKK (Wegpunkt) -> BGSF — DIAGONAL.

Trajektorienbasierte Auswertung: jeder Routenpunkt wird zu seiner
Ueberflugzeit bewertet (zeitlich zwischen den 3-h-Modellschritten
interpoliert), nicht als synoptischer Schnappschuss. Drei Szenarien,
verankert an der ETA BGSF (Standard 1430/1530/1630Z), komplette
Rueckwaertsrechnung mit 60%-Leistung und Modellwind je Segment.

Geschwindigkeiten (AFM 5.3.11, >1999 kg, ISA+10): FL100 60% TAS 152 kt,
FL195 60% TAS 165 kt. Climb-Segment vereinfachend mit FL195-TAS
(Fehler < ~5 min auf die Ueberflugzeiten).

Gates je Szenario (Bewertung an den Ueberflugzeiten):
  G1  BIRK + Strasse-Mitte auf FL100 (FZLVL>=11000 ft ODER 700 hPa trocken)
  G2  BGKK/Climb-Sektor: 700+500 hPa trocken
  G3  Kappe + BGSF auf FL195: kein Ice-Flag 500/FL195

Aufruf: greenland_leg.py [YYYY-MM-DD] [ETA1,ETA2,... als HHMM]
Ice-Flags RH-basiert und bewusst ueberwarnend (ECMWF-Glazierungs-Bias).
Planungshilfe — ersetzt kein amtliches Briefing, keine PIC-Entscheidung.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

UA = {"User-Agent": "TTA-GreenlandLeg/2.0 (private expedition briefing)"}
AWC = "https://aviationweather.gov/api/data"

LEVELS = [850, 700, 500, 400]
LEVEL_FT = {850: 4780, 700: 9880, 500: 18280, 400: 23570}
FL195_FT = 19500

ICE_T_MAX, ICE_T_MIN = 0.0, -16.0
ICE_RH, ICE_RH_MOD = 85.0, 95.0

TAS_LOW, TAS_HIGH = 152.0, 165.0        # kt, AFM 60% FL100 / FL195, ISA+10
HW_GUESS = 15.0                         # kt, nur fuer Grobplanung/Step-Wahl

@dataclass(frozen=True)
class WPT:
    name: str
    lat: float
    lon: float

ROUTE = [
    WPT("BIRK Reykjavik",      64.13, -21.94),
    WPT("Strasse-Mitte",       65.30, -30.00),
    WPT("BGKK Kulusuk (WPT)",  65.57, -37.12),
    WPT("Kappe ~66N43W",       66.30, -43.00),
    WPT("BGSF Kangerlussuaq",  67.01, -50.72),
]
IDX_BGKK = 2
STATIONS = ("BIRK", "BGKK", "BGSF")


def die(msg: str) -> None:
    print(f"ABBRUCH: {msg}", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------- Geometrie
def gc_nm(a: WPT, b: WPT) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    return math.acos(min(1.0, math.sin(p1) * math.sin(p2)
                         + math.cos(p1) * math.cos(p2) * math.cos(dl))) * 3440.065


def course_true(a: WPT, b: WPT) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0

SEG_NM = [gc_nm(ROUTE[i], ROUTE[i + 1]) for i in range(len(ROUTE) - 1)]
SEG_TC = [course_true(ROUTE[i], ROUTE[i + 1]) for i in range(len(ROUTE) - 1)]


# ------------------------------------------------------------------- Zeiten
def flight_day(now: datetime, override: str | None) -> datetime:
    if override:
        return datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (now + timedelta(days=1) if now.hour >= 12 else now).replace(
        hour=0, minute=0, second=0, microsecond=0)


def parse_etas(day: datetime, arg: str | None) -> list[datetime]:
    raw = (arg or "1430,1530,1630").split(",")
    out = []
    for s in raw:
        s = s.strip()
        if len(s) != 4 or not s.isdigit():
            die(f"ETA '{s}' nicht als HHMM lesbar")
        out.append(day.replace(hour=int(s[:2]), minute=int(s[2:])))
    return out


def rough_times(eta_bgsf: datetime) -> list[datetime]:
    """Grobe Ueberflugzeiten, rueckwaerts vom Ziel (nur Step-Auswahl)."""
    t = [eta_bgsf] * len(ROUTE)
    cur = eta_bgsf
    for i in range(len(ROUTE) - 2, -1, -1):
        tas = TAS_HIGH if i >= IDX_BGKK else TAS_LOW
        cur = cur - timedelta(hours=SEG_NM[i] / (tas - HW_GUESS))
        t[i] = cur
    return t


# --------------------------------------------------------------- ECMWF-Daten
def fetch_ecmwf(all_rough: list[datetime]) -> tuple[dict, list[datetime], str]:
    from ecmwf.opendata import Client
    import xarray as xr

    client = Client(source="ecmwf")
    req = dict(type="fc", stream="oper", levtype="pl",
               param=["t", "r", "u", "v"], levelist=LEVELS)
    latest = client.latest(**req)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)

    lo = min(all_rough) - timedelta(hours=1)
    hi = max(all_rough) + timedelta(hours=1)
    s0 = int((lo - latest).total_seconds() // 3600) // 3 * 3
    s1 = (int((hi - latest).total_seconds() // 3600) + 3) // 3 * 3
    if s0 < 0:
        die(f"Fenster vor juengstem Lauf {latest:%d.%H}Z")
    if s1 > 48:
        die(f"Steps bis +{s1} h: jenseits 48 h nur 6-h-Raster — Lauf zu alt "
            f"({latest:%d.%H}Z) oder Datum pruefen")
    steps = list(range(s0, s1 + 1, 3))
    grid = [latest + timedelta(hours=s) for s in steps]

    target = "ecmwf_leg.grib2"
    client.retrieve(date=latest.strftime("%Y%m%d"), time=latest.hour,
                    step=steps, target=target, **req)
    ds = xr.open_dataset(target, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    data: dict = {}
    for s, vt in zip(steps, grid):
        sel = ds.sel(step=timedelta(hours=s))
        data[vt] = {}
        for w in ROUTE:
            lon = w.lon % 360 if float(ds.longitude.max()) > 180 else w.lon
            p = sel.sel(latitude=w.lat, longitude=lon, method="nearest")
            data[vt][w.name] = {
                lvl: {"t": float(p.sel(isobaricInhPa=lvl)["t"]) - 273.15,
                      "rh": float(p.sel(isobaricInhPa=lvl)["r"]),
                      "u": float(p.sel(isobaricInhPa=lvl)["u"]),
                      "v": float(p.sel(isobaricInhPa=lvl)["v"])}
                for lvl in LEVELS}
    return data, grid, f"ECMWF oper {latest:%Y%m%d %H}Z"


def prof_at(data: dict, grid: list[datetime], wname: str, t: datetime) -> dict:
    """Profil eines Punkts zur Zeit t — linear zwischen Gitterzeiten."""
    if t <= grid[0]:
        return data[grid[0]][wname]
    if t >= grid[-1]:
        return data[grid[-1]][wname]
    for a, b in zip(grid, grid[1:]):
        if a <= t <= b:
            f = (t - a).total_seconds() / (b - a).total_seconds()
            pa, pb = data[a][wname], data[b][wname]
            return {lvl: {k: pa[lvl][k] + f * (pb[lvl][k] - pa[lvl][k])
                          for k in ("t", "rh", "u", "v")}
                    for lvl in LEVELS}
    return data[grid[-1]][wname]


# ------------------------------------------------------------- Meteo-Ableitung
def fzlvl_ft(prof: dict) -> float | None:
    t850, t700 = prof[850]["t"], prof[700]["t"]
    if t850 <= 0:
        return 0.0 if t850 == 0 else LEVEL_FT[850] * 0
    if t700 >= 0:
        t500 = prof[500]["t"]
        if t500 >= 0:
            return LEVEL_FT[500]
        f = t700 / (t700 - t500)
        return LEVEL_FT[700] + f * (LEVEL_FT[500] - LEVEL_FT[700])
    f = t850 / (t850 - t700)
    return LEVEL_FT[850] + f * (LEVEL_FT[700] - LEVEL_FT[850])


def ice_flag(t: float, rh: float) -> str:
    if ICE_T_MIN <= t <= ICE_T_MAX:
        if rh >= ICE_RH_MOD:
            return "ICE!"
        if rh >= ICE_RH:
            return "ICE?"
    return "-"


def t_at_fl195(prof: dict) -> tuple[float, float]:
    f = (FL195_FT - LEVEL_FT[500]) / (LEVEL_FT[400] - LEVEL_FT[500])
    return (prof[500]["t"] + f * (prof[400]["t"] - prof[500]["t"]),
            prof[500]["rh"] + f * (prof[400]["rh"] - prof[500]["rh"]))


def headwind(prof: dict, lvl: int, tc: float) -> float:
    rad = math.radians(tc)
    return -(prof[lvl]["u"] * math.sin(rad)
             + prof[lvl]["v"] * math.cos(rad)) * 1.9438


# ------------------------------------------------------------------ Szenario
def refine_times(data, grid, eta_bgsf: datetime) -> list[datetime]:
    """Ueberflugzeiten mit Modellwind je Segment, rueckwaerts vom Ziel
    (2 Iterationen). Segment >= BGKK: FL195/TAS 165, davor FL100/TAS 152."""
    t = rough_times(eta_bgsf)
    for _ in range(2):
        nt = [None] * len(ROUTE)
        nt[-1] = eta_bgsf
        cur = eta_bgsf
        for i in range(len(ROUTE) - 2, -1, -1):
            high = i >= IDX_BGKK
            tas = TAS_HIGH if high else TAS_LOW
            lvl = 500 if high else 700
            mid = cur - timedelta(hours=SEG_NM[i] / (2 * tas))
            hw = headwind(prof_at(data, grid, ROUTE[i].name, mid), lvl, SEG_TC[i])
            gs = max(tas - hw, 60.0)
            cur = cur - timedelta(hours=SEG_NM[i] / gs)
            nt[i] = cur
        t = nt
    return t


def scenario(data, grid, eta_bgsf: datetime) -> list[str]:
    times = refine_times(data, grid, eta_bgsf)
    profs = [prof_at(data, grid, w.name, tt) for w, tt in zip(ROUTE, times)]

    # Gates an den Ueberflugzeiten
    g1 = "OK"
    for i in (0, 1):
        fz = fzlvl_ft(profs[i]) or 0
        rh = profs[i][700]["rh"]
        if fz < 11000 and rh >= ICE_RH:
            g1 = "NOGO" if rh >= ICE_RH_MOD else max(g1, "WARN", key=len)
    kk = profs[IDX_BGKK]
    rhmax = max(kk[700]["rh"], kk[500]["rh"])
    g2 = "OK" if rhmax < ICE_RH else ("WARN" if rhmax < ICE_RH_MOD else "NOGO")
    g3 = "OK"
    for i in (3, 4):
        t195, rh195 = t_at_fl195(profs[i])
        flags = (ice_flag(t195, rh195),
                 ice_flag(profs[i][500]["t"], profs[i][500]["rh"]))
        if "ICE!" in flags:
            g3 = "NOGO"
        elif "ICE?" in flags and g3 == "OK":
            g3 = "WARN"
    rank = {"OK": 1, "WARN": 2, "NOGO": 3}
    tot = max((g1, g2, g3), key=lambda x: rank[x])

    out = ["", f"SZENARIO ETA BGSF {eta_bgsf:%H%M}Z  "
               f"(ETD BIRK ~{times[0]:%H%M}Z, BGKK ~{times[IDX_BGKK]:%H%M}Z)",
           f"{eta_bgsf:%d. %H%M}Z  G1-Leg1-FL100: {g1:4s}  G2-Climb: {g2:4s}  "
           f"G3-FL195: {g3:4s}  => [{tot}]",
           f"{'Punkt':22s} {'Ueberflug':>9s} {'Level':>6s} "
           f"{'T':>6s} {'RH':>4s} {'Flag':>4s} | {'FZLVL':>6s} | HW"]
    for i, (w, tt, p) in enumerate(zip(ROUTE, times, profs)):
        if i < IDX_BGKK:
            lvl_lbl, t, rh = "FL100", p[700]["t"], p[700]["rh"]
            hw = headwind(p, 700, SEG_TC[min(i, len(SEG_TC) - 1)])
        elif i == IDX_BGKK:
            lvl_lbl, t, rh = "Climb", p[500]["t"], max(p[700]["rh"], p[500]["rh"])
            hw = headwind(p, 500, SEG_TC[i])
        else:
            lvl_lbl = "FL195"
            t, rh = t_at_fl195(p)
            hw = headwind(p, 500, SEG_TC[i - 1])
        fz = fzlvl_ft(p)
        out.append(f"{w.name:22s} {tt:%H%M}Z{'':4s}{lvl_lbl:>6s} "
                   f"{t:+5.1f}C {rh:3.0f}% {ice_flag(t, rh):>4s} | "
                   f"{fz and f'{fz:5.0f}' or '  n/a':>6s} | {hw:+4.0f}kt")
    return out


# --------------------------------------------------------------------- METAR
def fetch_metar_taf() -> list[str]:
    out = ["", "METAR/TAF (aviationweather.gov)", "=" * 60]
    try:
        with httpx.Client(timeout=30, headers=UA) as c:
            for kind in ("metar", "taf"):
                r = c.get(f"{AWC}/{kind}",
                          params={"ids": ",".join(STATIONS), "format": "raw"})
                r.raise_for_status()
                out += [ln for ln in r.text.splitlines() if ln.strip()]
                out.append("")
    except httpx.HTTPError as e:
        out.append(f"(AWC nicht erreichbar: {e})")
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    day = flight_day(now, sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None)
    etas = parse_etas(day, sys.argv[2] if len(sys.argv) > 2 else None)

    all_rough = [t for e in etas for t in rough_times(e)]
    data, grid, run_label = fetch_ecmwf(all_rough)

    print("GREENLAND-LEG-BRIEFING  BIRK -> BGKK(WPT) -> BGSF  [DIAGONAL]")
    print(f"erzeugt {now:%Y-%m-%d %H:%M} UTC | Quelle {run_label}")
    print(f"60% Leistung (AFM: FL100 TAS {TAS_LOW:.0f}, FL195 TAS "
          f"{TAS_HIGH:.0f}), Wind je Segment aus Modell. Flugtag {day:%d.%m.%Y}.")
    print("Jeder Punkt zu seiner Ueberflugzeit bewertet (zeitinterpoliert).")
    print("Flags RH-basiert/ueberwarnend. Karten bleiben Pflicht: WAFS-Grids,")
    print("SIGWX, IMO/CFPS, Satellit Kulusuk-Sektor.")
    for e in etas:
        for ln in scenario(data, grid, e):
            print(ln)
    for ln in fetch_metar_taf():
        print(ln)


if __name__ == "__main__":
    main()
