#!/usr/bin/env python3
"""Greenland-Leg-Briefing RUECKFLUG BGSF -> BGKK (WPT) -> BIRK — DIAGONAL.

Ersetzt den Hinflug-Evaluator. Trajektorienbasierte Auswertung, jeder
Routenpunkt zu seiner Ueberflugzeit bewertet (linear zwischen den
3-h-Modellschritten interpoliert). Drei Szenarien, verankert an der
ETA BIRK (Standard 1430/1530/1630Z), Rueckwaertsrechnung nach BGSF.

HOEHENSEGMENTE (je Phase Optionen, alle parallel bewertet):
  CROSS BGSF -> Kappe -> BGKK:   FL190, FL130
  EAST  BGKK -> Mitte -> BIRK:   FL190, FL130, FL090

T/RH je Punkt und FL linear in ft zwischen den Druckflaechen
850/700/500/400 interpoliert; zusaetzlich konservativ das Flag einer
Standardflaeche innerhalb +/-1500 ft des FL beruecksichtigt
(FL190<->500 hPa, FL090<->700 hPa). Die alte FZLVL-Ausnahme ist
implizit: T > 0 am FL ergibt kein Ice-Flag.

TAS 60% (aus AFM-Ankern FL100 152 kt / FL195 165 kt, ISA+10, linear
ABGELEITET — keine AFM-Zitate): FL190 164, FL130 156, FL090 151 kt.
Timing-Referenz je Phase mit Mittelwert (CROSS 160 / EAST 154 kt);
Spreizung zwischen Hoehenkombinationen < ~15 min und damit unterhalb
der 3-h-Modellaufloesung.

Gesamtstatus je Szenario: je Phase die BESTE Hoehenoption, Gesamt =
schlechtere der beiden Phasen-Besten ("gibt es eine gangbare
Hoehenkombination?"). Praeferenz bei Gleichstand: CROSS hoch (FL190,
Terrain/Ice-frei), EAST tief (FL090, on-top Strategie).

Aufruf: greenland_leg.py [YYYY-MM-DD] [ETA1,ETA2,... als HHMM = ETA BIRK]
Ice-Flags RH-basiert und bewusst ueberwarnend (ECMWF-Glazierungs-Bias).
Planungshilfe — ersetzt kein amtliches Briefing, keine PIC-Entscheidung.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

UA = {"User-Agent": "TTA-GreenlandReturn/2.0 (private expedition briefing)"}
AWC = "https://aviationweather.gov/api/data"
OUT_TXT = "docs/greenland.txt"           # ANPASSEN: Pfad wie bisheriges Briefing

LEVELS = [850, 700, 500, 400]
LEVEL_FT = {850: 4780, 700: 9880, 500: 18280, 400: 23570}

ICE_T_MAX, ICE_T_MIN = 0.0, -16.0
ICE_RH, ICE_RH_MOD = 85.0, 95.0

FL_FT = {"F190": 19000, "F130": 13000, "F090": 9000}
FL_TAS = {"F190": 164.0, "F130": 156.0, "F090": 151.0}   # abgeleitet, s.o.
CROSS_FLS = ("F190", "F130")
EAST_FLS = ("F190", "F130", "F090")
NEAR_LVL = {"F190": 500, "F090": 700}    # Standardflaeche binnen 1500 ft
TAS_REF_CROSS, TAS_REF_EAST = 160.0, 154.0
HW_GUESS = -10.0                         # kt, Grobplanung: Westlage=Rueckenwind

RANK = {"-": 0, "ICE?": 1, "ICE!": 2}
STATUS = {0: "OK", 1: "WARN", 2: "NOGO"}

@dataclass(frozen=True)
class WPT:
    name: str
    lat: float
    lon: float

ROUTE = [
    WPT("BGSF Kangerlussuaq",  67.01, -50.72),
    WPT("Kappe ~66N43W",       66.30, -43.00),
    WPT("BGKK Kulusuk (WPT)",  65.57, -37.12),
    WPT("Strasse-Mitte",       65.30, -30.00),
    WPT("BIRK Reykjavik",      64.13, -21.94),
]
IDX_BGKK = 2
STATIONS = ("BGSF", "BGKK", "BIRK")
SEG_TAS = [TAS_REF_CROSS, TAS_REF_CROSS, TAS_REF_EAST, TAS_REF_EAST]
SEG_LVL = [500, 500, 700, 700]           # Windniveau Timing-Referenz


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


def seg_headwind(prof: dict, lvl: int, tc: float) -> float:
    u, v = prof[lvl]["u"], prof[lvl]["v"]
    rad = math.radians(tc)
    return -(u * math.sin(rad) + v * math.cos(rad)) * 1.9438


def times_from_eta(eta_birk: datetime,
                   wind_fn=None) -> tuple[list[datetime], list[float]]:
    """Ueberflugzeiten, rueckwaerts vom Anker ETA BIRK (Timing-Referenz)."""
    hw = [HW_GUESS] * 4
    t = [eta_birk] * 5
    for _ in range(2 if wind_fn else 1):
        for i in range(3, -1, -1):
            gs = max(60.0, SEG_TAS[i] - hw[i])
            t[i] = t[i + 1] - timedelta(hours=SEG_NM[i] / gs)
        if wind_fn:
            for i in range(4):
                mid = t[i] + (t[i + 1] - t[i]) / 2
                hw[i] = wind_fn(i, mid)
    return t, hw


# ------------------------------------------------------------- Ableitungen
def fzlvl_ft(prof: dict) -> int | None:
    lv = sorted(LEVELS, reverse=True)
    for lo, hi in zip(lv, lv[1:]):
        t1, t2 = prof[lo]["t"], prof[hi]["t"]
        if t1 >= 0.0 > t2:
            f = t1 / (t1 - t2)
            return int(LEVEL_FT[lo] + f * (LEVEL_FT[hi] - LEVEL_FT[lo]))
    if prof[850]["t"] < 0.0:
        return 0
    return None


def ice_flag(t: float, rh: float) -> str:
    if ICE_T_MIN <= t <= ICE_T_MAX:
        if rh >= ICE_RH_MOD:
            return "ICE!"
        if rh >= ICE_RH:
            return "ICE?"
    return "-"


def at_ft(prof: dict, ft: int) -> tuple[float, float]:
    """T/RH auf Hoehe ft, linear zwischen den Druckflaechen."""
    lv = sorted(LEVELS, reverse=True)                    # 850..400 aufsteigend ft
    if ft <= LEVEL_FT[lv[0]]:
        return prof[lv[0]]["t"], prof[lv[0]]["rh"]
    for lo, hi in zip(lv, lv[1:]):
        if LEVEL_FT[lo] <= ft <= LEVEL_FT[hi]:
            f = (ft - LEVEL_FT[lo]) / (LEVEL_FT[hi] - LEVEL_FT[lo])
            return (prof[lo]["t"] + f * (prof[hi]["t"] - prof[lo]["t"]),
                    prof[lo]["rh"] + f * (prof[hi]["rh"] - prof[lo]["rh"]))
    return prof[400]["t"], prof[400]["rh"]


def fl_eval(prof: dict, fl: str) -> tuple[str, float, float]:
    """(Flag, T, RH) am FL; konservativ inkl. naher Standardflaeche."""
    t, rh = at_ft(prof, FL_FT[fl])
    flag = ice_flag(t, rh)
    near = NEAR_LVL.get(fl)
    if near is not None:
        f2 = ice_flag(prof[near]["t"], prof[near]["rh"])
        if RANK[f2] > RANK[flag]:
            flag = f2
    return flag, t, rh


def interp_profile(data: dict, grid: list[datetime],
                   wname: str, when: datetime) -> dict:
    if when <= grid[0]:
        return data[grid[0]][wname]
    if when >= grid[-1]:
        return data[grid[-1]][wname]
    for g1, g2 in zip(grid, grid[1:]):
        if g1 <= when <= g2:
            f = (when - g1) / (g2 - g1)
            p1, p2 = data[g1][wname], data[g2][wname]
            return {lvl: {k: p1[lvl][k] + f * (p2[lvl][k] - p1[lvl][k])
                          for k in ("t", "rh", "u", "v")} for lvl in LEVELS}
    return data[grid[-1]][wname]


# --------------------------------------------------------------- Bewertung
def scenario(data: dict, grid: list[datetime], eta_birk: datetime) -> list[str]:
    def wind_fn(i: int, mid: datetime) -> float:
        prof = interp_profile(data, grid, ROUTE[i].name, mid)
        return seg_headwind(prof, SEG_LVL[i], SEG_TC[i])

    t, hw = times_from_eta(eta_birk, wind_fn)

    lines = ["", f"=== SZENARIO ETA BIRK {eta_birk:%H%M}Z  "
                 f"(ETD BGSF {t[0]:%H%M}Z, BGKK-Ueberflug {t[IDX_BGKK]:%H%M}Z) ===",
             f"{'Punkt':<21}{'Zeit':>6}{'FZLVL':>7}{'HW':>5}  "
             f"{'F190':^13}{'F130':^13}{'F090':^13}"]

    # Flag-Matrix: point_flags[i][fl]
    point_flags: list[dict] = []
    for i, w in enumerate(ROUTE):
        prof = interp_profile(data, grid, w.name, t[i])
        fz = fzlvl_ft(prof)
        fztxt = f"{fz:>6d}" if fz is not None else "  >400"
        hwtxt = f"{hw[i]:+4.0f}" if i < 4 else "    "
        fls = EAST_FLS if i >= IDX_BGKK else CROSS_FLS
        cells, flags = [], {}
        for fl in ("F190", "F130", "F090"):
            if fl in fls:
                flag, tt, rh = fl_eval(prof, fl)
                flags[fl] = flag
                cells.append(f"{tt:>5.1f}/{rh:>3.0f} {flag:<4}")
            else:
                cells.append(f"{'---':^13}")
        point_flags.append(flags)
        lines.append(f"{w.name:<21}{t[i]:%H%M}Z{fztxt:>7}{hwtxt:>5}  "
                     + "".join(f"{c:^13}" for c in cells))

    def phase(idx: tuple, fls: tuple) -> dict:
        return {fl: max(RANK[point_flags[i][fl]] for i in idx) for fl in fls}

    cross = phase((0, 1, 2), CROSS_FLS)
    east = phase((2, 3, 4), EAST_FLS)

    def desc_rank(flc: str, fle: str) -> int:
        """Descent-Transit bei BGKK durch die Spanne [FL_east, FL_cross]:
        RH-Kriterium der alten G2 auf allen Standardflaechen im Band."""
        lo, hi = sorted((FL_FT[fle], FL_FT[flc]))
        prof = interp_profile(data, grid, ROUTE[IDX_BGKK].name, t[IDX_BGKK])
        r = 0
        for lvl in LEVELS:
            if lo <= LEVEL_FT[lvl] <= hi:
                rh = prof[lvl]["rh"]
                r = max(r, 2 if rh >= ICE_RH_MOD else 1 if rh >= ICE_RH else 0)
        return r

    # Beste Kombination inkl. Descent-Transit; Praeferenz bei Gleichstand:
    # CROSS hoch (Terrain/Ice-frei), EAST tief (on-top Strategie)
    pref_c, pref_e = ("F190", "F130"), ("F090", "F130", "F190")
    combos = sorted(((max(cross[c], east[e], desc_rank(c, e)),
                      pref_c.index(c), pref_e.index(e), c, e)
                     for c in CROSS_FLS for e in EAST_FLS))
    tot_r, _, _, bc_fl, be_fl = combos[0]
    dsc = desc_rank(bc_fl, be_fl)

    lines.append("GATES  CROSS "
                 + " ".join(f"{fl}:{STATUS[cross[fl]]}" for fl in CROSS_FLS)
                 + " | EAST "
                 + " ".join(f"{fl}:{STATUS[east[fl]]}" for fl in EAST_FLS)
                 + f" | DESC:{STATUS[dsc]}"
                 + f" | BEST {bc_fl}+{be_fl} => [{STATUS[tot_r]}]")
    return lines


# ------------------------------------------------------------------- Daten
def fetch_ecmwf(day: datetime) -> tuple[dict, list[datetime]]:
    try:
        from ecmwf.opendata import Client
        import xarray as xr
        import tempfile, os
    except ImportError as e:
        die(f"Modul fehlt: {e}")
    client = Client(source="ecmwf")
    latest = client.latest(type="fc", param="t")
    if latest.tzinfo is None:                     # Bugfix wie gehabt
        latest = latest.replace(tzinfo=timezone.utc)
    steps, grid = [], []
    for h in range(6, 22, 3):
        vt = day.replace(hour=h)
        st = int((vt - latest).total_seconds() // 3600)
        if 0 <= st <= 90:
            steps.append(st)
            grid.append(vt)
    if not steps:
        die("Kein Modellschritt deckt den Flugtag")
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "gl.grib2")
        client.retrieve(type="fc", step=steps, levtype="pl",
                        levelist=LEVELS, param=["t", "r", "u", "v"], target=f)
        ds = xr.open_dataset(f, engine="cfgrib")
        data: dict = {}
        for vt, st in zip(grid, steps):
            data[vt] = {}
            sel = ds.sel(step=timedelta(hours=st))
            for w in ROUTE:
                p = sel.sel(latitude=w.lat, longitude=w.lon % 360,
                            method="nearest")
                data[vt][w.name] = {
                    lvl: {"t": float(p["t"].sel(isobaricInhPa=lvl)) - 273.15,
                          "rh": float(p["r"].sel(isobaricInhPa=lvl)),
                          "u": float(p["u"].sel(isobaricInhPa=lvl)),
                          "v": float(p["v"].sel(isobaricInhPa=lvl))}
                    for lvl in LEVELS}
    return data, grid


def fetch_metars() -> list[str]:
    out = ["", "METAR/TAF (AWC)", "-" * 60]
    try:
        with httpx.Client(headers=UA, timeout=30) as c:
            r = c.get(f"{AWC}/metar",
                      params={"ids": ",".join(STATIONS), "format": "raw",
                              "taf": "true", "hours": 3})
            out += [ln for ln in r.text.splitlines() if ln.strip()]
    except Exception as e:                        # noqa: BLE001
        out.append(f"(nicht abrufbar: {e})")
    return out


# -------------------------------------------------------------------- Main
def main() -> None:
    now = datetime.now(timezone.utc)
    day = flight_day(now, sys.argv[1] if len(sys.argv) > 1 else None)
    etas = parse_etas(day, sys.argv[2] if len(sys.argv) > 2 else None)

    data, grid = fetch_ecmwf(day)

    lines = [f"GREENLAND RETURN BGSF -> BGKK(WPT) -> BIRK — {day:%d.%m.%Y}",
             f"Erstellt {now:%d.%m. %H%M}Z | ECMWF oper 0.25 | "
             f"Segmente {' / '.join(f'{d:.0f}NM' for d in SEG_NM)} | "
             f"Kurse {' / '.join(f'{c:03.0f}T' for c in SEG_TC)}",
             "Hoehenoptionen: CROSS F190/F130 — EAST F190/F130/F090. "
             "Anker ETA BIRK; Timing-Referenz mittlere TAS, "
             "Kombinations-Spreizung < ~15 min.",
             "ACHTUNG F130 ueber der Kappe: Kappenkamm auf der Route "
             "~8500 ft — Freiraum ~4500 ft, Grid-MORA/MSA selbst pruefen; "
             "F130 liegt im moeglichen Wolkenband statt darueber."]
    for eta in etas:
        lines += scenario(data, grid, eta)
    lines += fetch_metars()
    lines += ["", "Ice-Flags RH-basiert, bewusst ueberwarnend. TAS-Werte "
                  "F190/F130/F090 aus AFM-Ankern abgeleitet, keine Zitate. "
                  "Planungshilfe, keine PIC-Entscheidung."]

    text = "\n".join(lines)
    print(text)
    try:
        with open(OUT_TXT, "w") as f:
            f.write(text + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
