#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EGPB -> BIRK Leg-Briefing (eigenstaendig, NWP-Cockpit unberuehrt).

Analog zu greenland_leg.py, aber fuer das Faeroer-Leg:
  EGPB Sumburgh -> (Faeroer/EKVG) -> Mid-Ocean -> BIRK Reykjavik
Levels FL080/100/120/140, Zielfenster 12-18Z (ETD 14Z).

Kernidee: Gegenwind-Leg auf WNW-Track -> tiefes Level = weniger Wind,
on-top ueber mariner Stratus. Das Script liefert je Routenpunkt/Level:
T, RH, Icing-Flag, On-Track-Headwind; dazu FZLVL und eine Level-Empfehlung
(tiefstes eisfreies Level mit tragbarem Gegenwind).

Ausgabe: reiner Text nach stdout (Workflow leitet in egpb_leg.txt um).
Aufruf:  python egpb_leg.py [YYYY-MM-DD]   (leer = morgen UTC)

Quellen: ECMWF Open Data (oper, 0.25 Grad) fuer T/RH/U/V auf Druckflaechen;
METAR/TAF via aviationweather.gov (AWC-API).
"""

import sys
import math
import datetime as dt

import httpx
from ecmwf.opendata import Client
import xarray as xr

# ----------------------------------------------------------------------------
# Route und Konfiguration
# ----------------------------------------------------------------------------
# (Name, lat, lon)  -- lon negativ = West
ROUTE = [
    ("EGPB Sumburgh",   59.879,  -1.296),
    ("Faeroer/EKVG",    62.060,  -7.280),
    ("Mid-Ocean",       63.100, -14.800),
    ("BIRK Reykjavik",  64.130, -21.941),
]

LEVELS_FL = [80, 100, 120, 140]          # zu bewertende Flugflaechen
# ECMWF Open Data fuehrt KEIN 600 hPa -> verfuegbaren Satz nutzen (wie greenland_leg).
# FL080~753 (850/700), FL100~697 (~700), FL120~644 & FL140~595 (interp 700<->500).
PL_RETRIEVE = [925, 850, 700, 500, 400]  # ECMWF-Druckflaechen zum Interpolieren
TARGET_HOURS = [12, 15, 18]              # Zielfenster (ETD 14Z liegt drin)

# Icing-Schwellen (wie greenland_leg): RH-basiert, bewusst ueberwarnend
ICE_TMIN, ICE_TMAX = -16.0, 0.0          # Temperaturband fuer Vereisung [C]
RH_ICE_Q, RH_ICE_X = 85.0, 95.0          # ICE?  bzw. ICE!

# Gate-Schwellen
HW_OK, HW_WARN = 30.0, 40.0              # bestes eisfreies Level: HWmax-Grenzen [kt]
ONTOP_FL = 80                            # FL fuer On-Top-Bewertung

MS_TO_KT = 1.94384
LAPSE = 0.0065
T0, P0 = 288.15, 1013.25
EXP = 5.25588

ECMWF_LICENSE = ("By downloading data from the ECMWF open data dataset, you agree to "
                 "the terms: Attribution 4.0 International (CC BY 4.0). "
                 "Please attribute ECMWF when downloading this data.")


# ----------------------------------------------------------------------------
# Kleine Atmosphaeren-/Geometrie-Helfer
# ----------------------------------------------------------------------------
def fl_to_hpa(fl):
    """Flugflaeche (Druckhoehe) -> Druck in hPa nach ISA."""
    h_m = fl * 100.0 * 0.3048
    return P0 * (1.0 - LAPSE * h_m / T0) ** EXP


def hpa_to_ft(p):
    """Druck -> Druckhoehe in ft nach ISA."""
    h_m = (T0 / LAPSE) * (1.0 - (p / P0) ** (1.0 / EXP))
    return h_m / 0.3048


def initial_bearing(lat1, lon1, lat2, lon2):
    """Grosskreis-Anfangskurs [Grad true] von Punkt 1 nach Punkt 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def track_bearings(route):
    """Kurs je Punkt = Anfangskurs zum naechsten Punkt; letzter erbt Vorsegment."""
    brgs = []
    for i in range(len(route)):
        a = route[i]
        b = route[i + 1] if i + 1 < len(route) else route[i]
        if i + 1 < len(route):
            brgs.append(initial_bearing(a[1], a[2], b[1], b[2]))
        else:
            brgs.append(brgs[-1])
    return brgs


def interp_logp(p_t, levels, values):
    """Log-p-Interpolation von 'values' (nach Druck 'levels') auf Zieldruck p_t."""
    pairs = sorted(zip(levels, values))          # aufsteigend nach Druck
    lo = None
    hi = None
    for p, v in pairs:
        if p <= p_t:
            lo = (p, v)
        if p >= p_t and hi is None:
            hi = (p, v)
    if lo is None:
        return pairs[0][1]
    if hi is None:
        return pairs[-1][1]
    (plo, vlo), (phi, vhi) = lo, hi
    if plo == phi:
        return vlo
    f = (math.log(p_t) - math.log(plo)) / (math.log(phi) - math.log(plo))
    return vlo + f * (vhi - vlo)


def freezing_level_ft(levels, temps_c):
    """FZLVL [ft]: tiefste Nulldurchgangs-Hoehe aus den Druckflaechen-Temps."""
    prof = sorted(((hpa_to_ft(p), t) for p, t in zip(levels, temps_c)))  # nach Hoehe
    for (h0, t0), (h1, t1) in zip(prof, prof[1:]):
        if (t0 >= 0.0 >= t1) and t0 != t1:
            f = (0.0 - t0) / (t1 - t0)
            return h0 + f * (h1 - h0)
    # kein Durchgang: ganz warm -> ueber Top, ganz kalt -> unter Boden
    if all(t > 0 for t in temps_c):
        return prof[-1][0]
    return prof[0][0]


def ice_flag(t_c, rh):
    if ICE_TMIN <= t_c <= ICE_TMAX:
        if rh >= RH_ICE_X:
            return "ICE!"
        if rh >= RH_ICE_Q:
            return "ICE?"
    return "-"


def headwind_kt(u_ms, v_ms, track_deg):
    """Positiver Wert = Gegenwind auf gegebenem Track."""
    d = math.radians(track_deg)
    tail = (u_ms * math.sin(d) + v_ms * math.cos(d))  # Rueckenwind-Komponente [m/s]
    return -tail * MS_TO_KT


# ----------------------------------------------------------------------------
# Laufauswahl / Datenbezug
# ----------------------------------------------------------------------------
def pick_run(now):
    """Juengster ECMWF-Zyklus (00/12Z), der >=7 h alt ist (Latenz)."""
    cands = []
    for back in range(0, 3):
        day = now.date() - dt.timedelta(days=back)
        for hh in (12, 0):
            cands.append(dt.datetime(day.year, day.month, day.day, hh))
    cands.sort(reverse=True)
    for c in cands:
        if (now - c).total_seconds() >= 7 * 3600:
            return c
    return cands[-1]


def retrieve(run, steps):
    client = Client(source="ecmwf")
    client.retrieve(
        date=run.strftime("%Y%m%d"),
        time=run.hour,
        stream="oper",
        type="fc",
        levtype="pl",
        levelist=PL_RETRIEVE,
        param=["t", "r", "u", "v"],
        step=steps,
        target="egpb.grib2",
    )
    ds = xr.open_dataset(
        "egpb.grib2",
        engine="cfgrib",
        backend_kwargs={"indexpath": "", "filter_by_keys": {"typeOfLevel": "isobaricInhPa"}},
    )
    return ds


def point_profile(ds, step_h, lat, lon):
    """Liefert dict level(hPa)->(t_C, rh, u_ms, v_ms) am naechsten Gitterpunkt."""
    lon360 = lon % 360.0
    sub = ds.sel(step=dt.timedelta(hours=step_h))
    sub = sub.sel(latitude=lat, longitude=lon360, method="nearest")
    prof = {}
    for p in PL_RETRIEVE:
        lv = sub.sel(isobaricInhPa=p)
        prof[p] = (
            float(lv["t"]) - 273.15,
            float(lv["r"]),
            float(lv["u"]),
            float(lv["v"]),
        )
    return prof


# ----------------------------------------------------------------------------
# METAR/TAF
# ----------------------------------------------------------------------------
def met_block(ids):
    out = []
    try:
        m = httpx.get("https://aviationweather.gov/api/data/metar",
                      params={"ids": ids, "format": "raw", "hours": 2}, timeout=30)
        t = httpx.get("https://aviationweather.gov/api/data/taf",
                      params={"ids": ids, "format": "raw"}, timeout=30)
        out += [ln for ln in m.text.splitlines() if ln.strip()]
        out.append("")
        out += [ln for ln in t.text.splitlines() if ln.strip()]
    except Exception as e:  # fail-loud, aber nicht das ganze Briefing killen
        out.append(f"MET n/a: {e}")
    return out


# ----------------------------------------------------------------------------
# Hauptlauf
# ----------------------------------------------------------------------------
def main():
    now = dt.datetime.utcnow()
    if len(sys.argv) > 1 and sys.argv[1].strip():
        target_date = dt.date.fromisoformat(sys.argv[1].strip())
    elif now.hour < TARGET_HOURS[-1]:
        # Zielfenster heute liegt noch (teils) vor uns -> heutiger Flugtag
        target_date = now.date()
    else:
        # spaeter Lauf (z.B. So 19:30Z) -> morgiger Flugtag
        target_date = (now + dt.timedelta(days=1)).date()

    run = pick_run(now)
    targets = [dt.datetime(target_date.year, target_date.month, target_date.day, h)
               for h in TARGET_HOURS]
    steps = []
    for tg in targets:
        s = int(round((tg - run).total_seconds() / 3600.0))
        if s < 0 or s > 144:
            print(f"WARNUNG: Zielzeit {tg:%d.%HZ} = Step {s} ausserhalb 0..144", file=sys.stderr)
        steps.append(s)

    ds = retrieve(run, steps)
    brgs = track_bearings(ROUTE)
    fl_hpa = {fl: fl_to_hpa(fl) for fl in LEVELS_FL}

    L = []
    L.append(ECMWF_LICENSE)
    L.append("EGPB-BIRK-LEG-BRIEFING  EGPB -> Faeroer -> BIRK")
    L.append(f"erzeugt {now:%Y-%m-%d %H:%M} UTC | Quelle ECMWF oper {run:%Y%m%d %H}Z")
    L.append("Strategie: Gegenwind-Leg WNW; tiefes Level = weniger Wind; on-top ueber")
    L.append(f"mariner St. Levels FL{'/'.join(str(f) for f in LEVELS_FL)}. "
             f"Zielfenster {target_date:%d.%m.} {TARGET_HOURS[0]}-{TARGET_HOURS[-1]}Z")
    L.append("HINWEIS: RH-basierte Ice-Flags = notwendige Bedingung, bewusst")
    L.append("ueberwarnend (ECMWF-Glazierungs-Bias). Karten bleiben Pflicht:")
    L.append("WAFS-Grids FL080/100/140, SIGWX, IMO/CFPS, Satellit Faeroer.")
    L.append("")
    L.append("GATES (automatisch, je Zeitschritt schlechtester Punkt)")
    L.append("=" * 60)

    detail_blocks = []
    for step_h, tg in zip(steps, targets):
        # Profile je Routenpunkt einsammeln
        rows = []  # (name, fzlvl, {fl:(t,rh,ice,hw)})
        for (name, lat, lon), brg in zip(ROUTE, brgs):
            prof = point_profile(ds, step_h, lat, lon)
            levs = list(prof.keys())
            temps = [prof[p][0] for p in levs]
            fz = freezing_level_ft(levs, temps)
            per_fl = {}
            for fl in LEVELS_FL:
                pt = fl_hpa[fl]
                t = interp_logp(pt, levs, [prof[p][0] for p in levs])
                rh = interp_logp(pt, levs, [prof[p][1] for p in levs])
                u = interp_logp(pt, levs, [prof[p][2] for p in levs])
                v = interp_logp(pt, levs, [prof[p][3] for p in levs])
                per_fl[fl] = (t, rh, ice_flag(t, rh), headwind_kt(u, v, brg))
            rows.append((name, fz, per_fl))

        # ---- Gate-Bewertung ----
        # G1: existiert ein Level ohne ICE! ueber ALLE Punkte?
        ice_free_levels = []
        for fl in LEVELS_FL:
            if all(r[2][fl][2] != "ICE!" for r in rows):
                ice_free_levels.append(fl)
        g1 = "OK" if ice_free_levels else "NOGO"

        # G2: On-Top FL080 plausibel? FZLVL <= FL080 und FL080 trocken (RH<85) irgendwo kritisch?
        ontop_ok = all((r[1] <= ONTOP_FL * 100) or (r[2][ONTOP_FL][1] < RH_ICE_Q) for r in rows)
        g2 = "OK" if ontop_ok else "WARN"

        # G3: bestes eisfreies Level -> kleinster HWmax; Ampel nach Schwelle
        best_fl, best_hw = None, None
        for fl in (ice_free_levels or LEVELS_FL):
            hwmax = max(r[2][fl][3] for r in rows)
            if best_hw is None or hwmax < best_hw:
                best_fl, best_hw = fl, hwmax
        if best_hw is None:
            g3 = "NOGO"
        elif best_hw <= HW_OK:
            g3 = "OK"
        elif best_hw <= HW_WARN:
            g3 = "WARN"
        else:
            g3 = "NOGO"

        order = {"OK": 0, "WARN": 1, "NOGO": 2}
        overall = max([g1, g2, g3], key=lambda s: order[s])
        best_txt = f"Best: FL{best_fl:03d} HWmax {best_hw:+.0f}kt" if best_fl else "Best: -"
        L.append(f"{tg:%d}. {tg:%H}Z  G1-Eisfrei-Lvl: {g1:<4} "
                 f"G2-OnTop080: {g2:<4} G3-Wind: {g3:<4} => [{overall}]  {best_txt}")

        # ---- Detailblock ----
        db = []
        db.append(f"--- {tg:%d.%m.} {tg:%H}Z (Step +{step_h}h) ---")
        hdr = f"{'Punkt':<18}{'FZLVL':>7} |"
        for fl in LEVELS_FL:
            hdr += f"  FL{fl:03d} T/RH/ICE  HW |"
        db.append(hdr)
        for name, fz, per_fl in rows:
            line = f"{name:<18}{fz:>6.0f}ft |"
            for fl in LEVELS_FL:
                t, rh, ice, hw = per_fl[fl]
                line += f" {t:+5.1f}C {rh:3.0f}% {ice:<4} {hw:+4.0f} |"
            db.append(line)
        detail_blocks.append("\n".join(db))

    L.append("")
    L.append("G1: eisfreies Level vorhanden | G2: FZLVL<=FL080 oder FL080 trocken | "
             "G3: bestes eisfreies Level HWmax<=30kt")
    L.append("")
    L.extend([""] + detail_blocks)
    L.append("")
    L.append("METAR/TAF (aviationweather.gov)")
    L.append("=" * 60)
    L.extend(met_block("EGPB,EKVG,BIRK,BIKF,EGPC"))

    print("\n".join(L))


if __name__ == "__main__":
    main()
