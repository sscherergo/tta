#!/usr/bin/env python3
"""Greenland-Leg-Briefing BIRK -> BGKK (Wegpunkt) -> BGSF.

Zieht ECMWF Open Data (0.25 Grad, oper) fuer T/RH/Wind auf
850/700/500/400 hPa an fuenf Routenpunkten, leitet FZLVL und
Icing-Flags ab und bewertet die drei Gates der FL100/FL195-Strategie:

  G1  Leg 1 BIRK->Kueste auf FL100 unter/ausserhalb des Icings
      (FZLVL >= 11.000 ft ODER 700-hPa-Schicht trocken)
  G2  Climb-Sektor vor der Kueste frei (700+500 hPa trocken)
  G3  Querung FL195 ueber dem Icing (500/400 hPa ohne Ice-Flag)

Dazu METAR/TAF BIRK/BGKK/BGSF (AWC-API). Ausgabe: Textbriefing stdout.

HINWEIS: ECMWF-Wolkenphysik glaziert arktische Mischphasenwolken zu
frueh -> RH-basierte Flags hier bewusst konservativ (ueberwarnend).
Planungshilfe, ersetzt kein amtliches Briefing und keine PIC-Entscheidung.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

UA = {"User-Agent": "TTA-GreenlandLeg/1.0 (private expedition briefing)"}
AWC = "https://aviationweather.gov/api/data"

LEVELS = [850, 700, 500, 400]          # hPa
# ISA-Druckhoehen der Flaechen (ft), fuer Anzeige/Interpolation
LEVEL_FT = {850: 4780, 700: 9880, 500: 18280, 400: 23570}
FL195_FT = 19500

# Icing-Flag: notwendige Bedingungen unterkuehlt + nahe Saettigung.
ICE_T_MAX, ICE_T_MIN = 0.0, -16.0      # degC
ICE_RH = 85.0                          # % -> ICE?
ICE_RH_MOD = 95.0                      # % -> ICE!

TRACK_TRUE = 285.0                     # grober Streckenkurs fuer HW

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
STATIONS = ("BIRK", "BGKK", "BGSF")


def die(msg: str) -> None:
    print(f"ABBRUCH: {msg}", file=sys.stderr)
    sys.exit(1)


def target_window(now: datetime, override: str | None) -> list[datetime]:
    """Zielfenster 06-15Z des Flugtags (heute ab 12Z-Vorabend-Sicht:
    morgen; vormittags: heute). 3-h-Raster."""
    if override:
        day = datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        day = now + timedelta(days=1) if now.hour >= 12 else now
    base = day.replace(hour=6, minute=0, second=0, microsecond=0)
    return [base + timedelta(hours=h) for h in (0, 3, 6, 9)]


def fetch_ecmwf(times: list[datetime]) -> tuple[dict, str]:
    """Neuesten oper-Lauf holen, der das Fenster abdeckt.
    Rueckgabe: data[valid][wpt][level] = {t, rh, u, v}; Label des Laufs."""
    from ecmwf.opendata import Client          # pip install ecmwf-opendata
    import xarray as xr

    client = Client(source="ecmwf")
    req = dict(type="fc", stream="oper", levtype="pl",
               param=["t", "r", "u", "v"], levelist=LEVELS)
    latest = client.latest(**req)              # datetime des juengsten Laufs
    steps = sorted({int((vt - latest).total_seconds() // 3600) for vt in times})
    if steps[0] < 0:
        die(f"Zielfenster liegt vor dem juengsten Lauf {latest:%d.%H}Z")
    if steps[-1] > 144:
        die(f"Zielfenster jenseits +144h des Laufs {latest:%d.%H}Z")

    target = "ecmwf_leg.grib2"
    client.retrieve(date=latest.strftime("%Y%m%d"), time=latest.hour,
                    step=steps, target=target, **req)

    ds = xr.open_dataset(target, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    out: dict = {}
    for vt in times:
        step_h = int((vt - latest).total_seconds() // 3600)
        sel_t = ds.sel(step=timedelta(hours=step_h))
        out[vt] = {}
        for w in ROUTE:
            lon = w.lon % 360 if float(ds.longitude.max()) > 180 else w.lon
            p = sel_t.sel(latitude=w.lat, longitude=lon, method="nearest")
            out[vt][w.name] = {}
            for lvl in LEVELS:
                q = p.sel(isobaricInhPa=lvl)
                out[vt][w.name][lvl] = {
                    "t": float(q["t"]) - 273.15,
                    "rh": float(q["r"]),
                    "u": float(q["u"]),
                    "v": float(q["v"]),
                }
    return out, f"ECMWF oper {latest:%Y%m%d %H}Z"


def fzlvl_ft(prof: dict) -> float | None:
    """0-Grad-Hoehe aus T850/T700 (linear in Druckhoehe). None = unklar."""
    t850, t700 = prof[850]["t"], prof[700]["t"]
    if t850 <= 0:
        return LEVEL_FT[850] * (1 if t850 == 0 else 0)   # unterhalb 850
    if t700 >= 0:
        # oberhalb 700: mit 500 weiterschauen
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
    """T und RH auf FL195 zwischen 500 und 400 hPa interpoliert."""
    f = (FL195_FT - LEVEL_FT[500]) / (LEVEL_FT[400] - LEVEL_FT[500])
    t = prof[500]["t"] + f * (prof[400]["t"] - prof[500]["t"])
    rh = prof[500]["rh"] + f * (prof[400]["rh"] - prof[500]["rh"])
    return t, rh


def headwind(prof: dict, lvl: int) -> float:
    import math
    u, v = prof[lvl]["u"], prof[lvl]["v"]
    rad = math.radians(TRACK_TRUE)
    return -(u * math.sin(rad) + v * math.cos(rad)) * 1.9438  # kt, + = Gegenwind


def gates(data: dict, times: list[datetime]) -> list[str]:
    lines = ["", "GATES (automatisch, je Zeitschritt schlechtester Punkt)",
             "=" * 60]
    for vt in times:
        d = data[vt]
        # G1: BIRK + Strasse-Mitte auf FL100
        g1 = "OK"
        for wn in (ROUTE[0].name, ROUTE[1].name):
            fz = fzlvl_ft(d[wn]) or 0
            wet = d[wn][700]["rh"] >= ICE_RH
            if fz < 11000 and wet:
                g1 = "NOGO" if d[wn][700]["rh"] >= ICE_RH_MOD else "WARN"
        # G2: Climb-Sektor BGKK 700+500 trocken
        kk = d[ROUTE[2].name]
        rhmax = max(kk[700]["rh"], kk[500]["rh"])
        g2 = "OK" if rhmax < ICE_RH else ("WARN" if rhmax < ICE_RH_MOD else "NOGO")
        # G3: Kappe+BGSF auf FL195 (500/400) ohne Ice
        g3 = "OK"
        for wn in (ROUTE[3].name, ROUTE[4].name):
            t195, rh195 = t_at_fl195(d[wn])
            fl = ice_flag(t195, rh195)
            f500 = ice_flag(d[wn][500]["t"], d[wn][500]["rh"])
            if "ICE!" in (fl, f500):
                g3 = "NOGO"
            elif "ICE?" in (fl, f500) and g3 == "OK":
                g3 = "WARN"
        total = ("NOGO" if "NOGO" in (g1, g2, g3)
                 else "WARN" if "WARN" in (g1, g2, g3) else "OK")
        lines.append(f"{vt:%d. %H}Z  G1-Leg1-FL100: {g1:4s}  "
                     f"G2-Climb: {g2:4s}  G3-FL195: {g3:4s}  => [{total}]")
    lines.append("")
    lines.append("G1: FZLVL>=11000ft oder 700hPa trocken | G2: RH(700,500)"
                 f"<{ICE_RH:.0f}% am Climb-Punkt | G3: kein Ice-Flag 500/FL195")
    return lines


def table(data: dict, times: list[datetime]) -> list[str]:
    lines = []
    for vt in times:
        lines += ["", f"--- {vt:%d.%m. %H}Z ---",
                  f"{'Punkt':22s} {'FZLVL':>6s} | {'FL100(700)':>14s} "
                  f"| {'FL180(500)':>14s} | {'FL195':>14s} | HW195"]
        for w in ROUTE:
            p = data[vt][w.name]
            fz = fzlvl_ft(p)
            t195, rh195 = t_at_fl195(p)
            hw = headwind(p, 500)
            def cell(t, rh):
                return f"{t:+5.1f}C {rh:3.0f}% {ice_flag(t, rh):4s}"
            lines.append(
                f"{w.name:22s} {fz and f'{fz:5.0f}' or '  n/a':>6s} | "
                f"{cell(p[700]['t'], p[700]['rh'])} | "
                f"{cell(p[500]['t'], p[500]['rh'])} | "
                f"{cell(t195, rh195)} | {hw:+4.0f}kt")
    return lines


def fetch_metar_taf() -> list[str]:
    ids = ",".join(STATIONS)
    out = ["", "METAR/TAF (aviationweather.gov)", "=" * 60]
    try:
        with httpx.Client(timeout=30, headers=UA) as c:
            for kind in ("metar", "taf"):
                r = c.get(f"{AWC}/{kind}", params={"ids": ids, "format": "raw"})
                r.raise_for_status()
                out += [ln for ln in r.text.splitlines() if ln.strip()]
                out.append("")
    except httpx.HTTPError as e:
        out.append(f"(AWC nicht erreichbar: {e})")
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    override = sys.argv[1] if len(sys.argv) > 1 else None
    times = target_window(now, override)
    data, run_label = fetch_ecmwf(times)

    print("GREENLAND-LEG-BRIEFING  BIRK -> BGKK(WPT) -> BGSF")
    print(f"erzeugt {now:%Y-%m-%d %H:%M} UTC | Quelle {run_label}")
    print(f"Strategie: FL100 bis vor Kueste, Climb ab ~40-60NM vor BGKK, "
          f"FL195 Querung. Zielfenster {times[0]:%d.%m.} "
          f"{times[0]:%H}-{times[-1]:%H}Z")
    print("HINWEIS: RH-basierte Ice-Flags = notwendige Bedingung, bewusst")
    print("ueberwarnend (ECMWF-Glazierungs-Bias). Karten bleiben Pflicht:")
    print("WAFS-Grids FL100/140/180, SIGWX, IMO/CFPS, Satellit Kulusuk.")
    for ln in gates(data, times):
        print(ln)
    for ln in table(data, times):
        print(ln)
    for ln in fetch_metar_taf():
        print(ln)


if __name__ == "__main__":
    main()
