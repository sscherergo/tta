"""
leg_briefing.py
===============

Fokussiertes Leg-Briefing aus dem juengsten Routen-Briefing plus live
abgerufenen METARs/TAFs. Laufzeit ~30 s (keine GRIB-Downloads: Modell-
daten kommen aus briefings/, nur die amtlichen Meldungen werden frisch
geholt).

Nutzung:
    python leg_briefing.py --leg CYHK-CYCB --etd 1300
    python leg_briefing.py --leg CYHK-CYCB --etd 1300 --alt CYYH,CYBB \
                           --tas 150

Ausgabe: leg.txt (aktuell) + legs/leg_<STAMP>.txt (10 juengste).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

AWC_API = "https://aviationweather.gov/api/data"
BRIEFING_DIR = Path("briefings")
OUT = Path("leg.txt")
ARCHIVE = Path("legs")
KEEP = 10

# Koordinaten und Standard-Anflug-Ausgangspunkte (Auszug aus der Route)
AIRPORTS: dict[str, tuple[str, float, float]] = {
    "CYFB": ("Iqaluit", 63.756, -68.556),
    "CYIO": ("Pond Inlet", 72.683, -77.967),
    "CYRB": ("Resolute Bay", 74.717, -94.969),
    "CYHK": ("Gjoa Haven", 68.636, -95.850),
    "CYCB": ("Cambridge Bay", 69.108, -105.138),
    "PABR": ("Utqiagvik", 71.285, -156.766),
    "PAOM": ("Nome", 64.512, -165.445),
    "CYCY": ("Clyde River", 70.486, -68.517),
    "CYAB": ("Arctic Bay", 73.006, -85.047),
    "CYYH": ("Taloyoak", 69.547, -93.577),
    "CYBB": ("Kugaaruk", 68.534, -89.808),
    "CYCO": ("Kugluktuk", 67.817, -115.144),
    "CYHI": ("Ulukhaktok", 70.763, -117.806),
    "CYPC": ("Paulatuk", 69.361, -124.075),
    "CYUB": ("Tuktoyaktuk", 69.433, -133.026),
    "CYEV": ("Inuvik", 68.304, -133.483),
    "PASC": ("Deadhorse", 70.195, -148.465),
    "PAWI": ("Wainwright", 70.638, -159.995),
    "PAPO": ("Point Hope", 68.349, -166.799),
    "PAOT": ("Kotzebue", 66.885, -162.599),
}
# ICAO -> Anflug-Ausgangspunkt, fuer den die HW-Spalte im Briefing gilt
BRIEFING_ORIGIN = {
    "CYIO": "CYFB", "CYRB": "CYIO", "CYHK": "CYRB", "CYCB": "CYHK",
    "PABR": "CYCB", "PAOM": "PABR", "CYCY": "CYFB", "CYAB": "CYIO",
    "CYYH": "CYRB", "CYBB": "CYRB", "CYCO": "CYCB", "CYHI": "CYCB",
    "CYPC": "CYCB", "CYUB": "CYCB", "CYEV": "CYCB", "PASC": "CYCB",
    "PAWI": "PABR", "PAPO": "PABR", "PAOT": "PABR",
}

# --- Briefing-Parser (Block 0, 6- und 7-Token-Format) ----------------------
HDR_RE = re.compile(r"^--- ([A-Z]{4}) ")
ROW_RE = re.compile(r"^\s*(\d{2})\. (\d{2})Z\s+(.+?)\s+\[(OK|WARN|NOGO)\]\s*$")
TOKEN_RE = re.compile(
    r"(>5000|[+\-]?\d+(?:\.\d+)?|—|\?)(~?)(?:\s+(OK|WARN|NOGO))?")
STAMP_RE = re.compile(r"briefing_(\d{8})T(\d{4})Z\.txt$")
RUN_RE = re.compile(r"VORHERSAGE: CAPS-Modelllauf (\d{8}) (\d{2}):00 UTC")


def resolve_valid(made, day, hour):
    for k in (0, 1):
        y = made.year + (made.month - 1 + k) // 12
        mo = (made.month - 1 + k) % 12 + 1
        try:
            cand = made.replace(year=y, month=mo, day=day, hour=hour,
                                minute=0, second=0, microsecond=0)
        except ValueError:
            continue
        if -6 * 3600 <= (cand - made).total_seconds() <= 60 * 3600:
            return cand
    return None


def load_latest_briefing() -> tuple[str, datetime]:
    files = sorted(BRIEFING_DIR.glob("briefing_*.txt")) \
        if BRIEFING_DIR.exists() else []
    if not files:
        sys.exit("Kein Briefing in briefings/ — erst den Briefing-Workflow "
                 "laufen lassen.")
    m = STAMP_RE.search(files[-1].name)
    made = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M") \
        .replace(tzinfo=timezone.utc)
    return files[-1].read_text(encoding="utf-8", errors="replace"), made


def parse_block0(text: str, made: datetime) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    icao = None
    for line in text.splitlines():
        if line.startswith("BLOCK 1"):
            break
        h = HDR_RE.match(line.strip())
        if h:
            icao = h.group(1) if h.group(1) in AIRPORTS else None
            continue
        if icao is None:
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        toks = TOKEN_RE.findall(m.group(3))
        if len(toks) == 7:
            hw, xw, cig, sp, trd_d, trd_p, ice = toks
            trd = (trd_d[0], "", trd_p[2])
        elif len(toks) == 6:
            hw, xw, cig, sp, trd, ice = toks
        else:
            continue
        valid = resolve_valid(made, int(m.group(1)), int(m.group(2)))
        if valid is None:
            continue
        rows.setdefault(icao, []).append(
            {"valid": valid, "line": line.rstrip(), "total": m.group(4),
             "hw": hw, "xw": xw, "cig": cig, "sp": sp, "trd": trd,
             "ice": ice})
    return rows


def nearest_row(rows: list[dict], t: datetime) -> dict | None:
    return min(rows, key=lambda r: abs((r["valid"] - t).total_seconds()),
               default=None) if rows else None


# --- Geometrie ---------------------------------------------------------------

def dist_tc(a: str, b: str) -> tuple[float, float]:
    _n1, la1, lo1 = AIRPORTS[a]
    _n2, la2, lo2 = AIRPORTS[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)) * 3440.1
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return d, math.degrees(math.atan2(x, y)) % 360


# --- Live METAR/TAF ----------------------------------------------------------

async def fetch_awc(kind: str, ids: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            r = await client.get(f"{AWC_API}/{kind}",
                                 params={"ids": ids, "format": "raw"},
                                 headers={"User-Agent": "leg-briefing/1.0"},
                                 timeout=45.0)
            return r.text.strip() if r.status_code == 200 else ""
        except httpx.HTTPError as exc:
            print(f"AWC {kind}: {exc}", file=sys.stderr)
            return ""


# --- Hauptlogik --------------------------------------------------------------

def parse_etd(s: str) -> datetime:
    now = datetime.now(timezone.utc)
    hh, mm = int(s[:2]), int(s[2:] or "0")
    etd = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if etd < now - timedelta(hours=2):
        etd += timedelta(days=1)          # gemeint ist morgen
    return etd


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leg", required=True, help="z.B. CYHK-CYCB")
    ap.add_argument("--etd", required=True, help="HHMM UTC, z.B. 1300")
    ap.add_argument("--alt", default="", help="Alternates, z.B. CYYH,CYBB")
    ap.add_argument("--tas", type=float, default=150.0)
    args = ap.parse_args()

    dep, arr = (s.strip().upper() for s in args.leg.split("-", 1))
    for icao in (dep, arr):
        if icao not in AIRPORTS:
            sys.exit(f"Unbekannter Platz: {icao}")
    alts = [a.strip().upper() for a in args.alt.split(",") if a.strip()]
    alts = [a for a in alts if a in AIRPORTS] or \
        [a for a in ("CYYH", "CYBB") if a not in (dep, arr)][:1]
    etd = parse_etd(args.etd)

    text, made = load_latest_briefing()
    rows = parse_block0(text, made)
    run = RUN_RE.search(text)
    run_txt = f"{run.group(1)} {run.group(2)}Z" if run else "unbekannt"

    dist, tc = dist_tc(dep, arr)
    hw_kt = None
    r_arr_probe = nearest_row(rows.get(arr, []), etd)
    if BRIEFING_ORIGIN.get(arr) == dep and r_arr_probe:
        m = re.match(r"([+\-]\d+)", r_arr_probe["hw"][0])
        if m:
            hw_kt = float(m.group(1))
    gs = max(args.tas - (hw_kt or 0.0), 60.0)
    ete_h = dist / gs
    eta = etd + timedelta(hours=ete_h)

    ids = ",".join(dict.fromkeys([dep, arr] + alts))
    metar_raw, taf_raw = await asyncio.gather(
        fetch_awc("metar", ids), fetch_awc("taf", ids))

    now = datetime.now(timezone.utc)
    age_h = (now - made).total_seconds() / 3600
    L = [f"LEG-BRIEFING {dep} \u2192 {arr}",
         f"ETD {etd:%d.%m. %H:%M}Z | Distanz {dist:.0f} nm | TC {tc:03.0f} "
         f"| ETE {int(ete_h)}:{int(ete_h % 1 * 60):02d} "
         f"(TAS {args.tas:.0f}"
         + (f", HW {hw_kt:+.0f} kt" if hw_kt is not None
            else ", HW unbekannt — kein Standardleg, Block 1 pruefen")
         + f") | ETA {eta:%H:%M}Z",
         f"Modell: CAPS-Lauf {run_txt} | Briefing erzeugt "
         f"{made:%d.%m. %H:%M}Z (Alter {age_h:.1f} h)"
         + ("  ⚠ ALT — vor Abflug Briefing-Workflow neu laufen lassen!"
            if age_h > 14 else ""),
         "METAR/TAF: live abgerufen "
         f"{now:%d.%m. %H:%M}Z",
         "HINWEIS: Planungshilfe — ersetzt kein amtliches Briefing und "
         "keine PIC-Entscheidung.", ""]

    L.append("=" * 70)
    L.append("DASHBOARD-ZEILEN (Ampeln zum jeweiligen Zeitpunkt)")
    for icao, t, role in ([(dep, etd, "ABFLUG"), (arr, eta, "ZIEL")]
                          + [(a, eta, "ALTERNATE") for a in alts]):
        r = nearest_row(rows.get(icao, []), t)
        L.append(f"\n{role} {icao} {AIRPORTS[icao][0]} — Soll {t:%H:%M}Z:")
        if r is None:
            L.append("  keine Dashboard-Daten im Briefing")
            continue
        L.append("  " + r["line"].strip())
        dt = abs((r["valid"] - t).total_seconds()) / 3600
        if dt > 3.5:
            L.append(f"  (naechste Zeile {dt:.0f} h entfernt — "
                     "Randbereich des Briefings)")

    L += ["", "=" * 70, "AKTUELLE METAR (live):",
          metar_raw or "(nicht abrufbar)"]
    L += ["", "AKTUELLE TAF (live):", taf_raw or "(nicht abrufbar)"]
    L += ["", "=" * 70,
          "Checkliste: METAR gegen Dashboard pruefen (Persistenz!), "
          "TAF-Aussage fuer ETA-Fenster lesen, Satelliten-Chips "
          f"sat/{dep}.jpg und sat/{arr}.jpg ansehen."]

    OUT.write_text("\n".join(L), encoding="utf-8")
    ARCHIVE.mkdir(exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%MZ")
    (ARCHIVE / f"leg_{stamp}.txt").write_text("\n".join(L), encoding="utf-8")
    for p in sorted(ARCHIVE.glob("leg_*.txt"))[:-KEEP]:
        p.unlink()
    print(f"Leg-Briefing geschrieben: {OUT} (+ Archiv leg_{stamp}.txt)")


if __name__ == "__main__":
    asyncio.run(main())
