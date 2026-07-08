"""
sat_chips.py
============

Stufe 1 der Satelliten-Integration: laedt fuer jeden Hauptplatz einen
VIIRS-Ausschnitt (~250 x 250 km) und legt True Color und Snow/Fog-RGB
nebeneinander in ein Bild: sat/{ICAO}.jpg

Quelle: NASA GIBS via Worldview-Snapshots-API (kein API-Key).
  True Color       : VIIRS_NOAA20_CorrectedReflectance_TrueColor
  Snow/Fog-RGB     : VIIRS_NOAA20_CorrectedReflectance_BandsM3-I3-M11
                     (Nebel/Wasserwolke = weiss, Schnee/Eis = tuerkis)
Tageskomposite, LANCE-Latenz ~3 h nach Ueberflug. Ist das heutige Bild
noch (teil)leer, wird automatisch auf den Vortag zurueckgegriffen.

Nutzung:
    python sat_chips.py                    # alle Hauptplaetze, heute
    python sat_chips.py CYRB CYIO          # Teilmenge
    python sat_chips.py --date 2026-07-06  # festes Datum

Abhaengigkeiten: httpx, pillow (beide bereits in requirements.txt)
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageStat

SNAPSHOT_API = "https://wvs.earthdata.nasa.gov/api/v1/snapshot"
LAYERS = [
    ("TrueColor", "VIIRS_NOAA20_CorrectedReflectance_TrueColor"),
    ("Snow/Fog-RGB", "VIIRS_NOAA20_CorrectedReflectance_BandsM3-I3-M11"),
]
FALLBACK_SAT = [("NOAA20", "NOAA21"), ("NOAA20", "SNPP")]  # bei Bildluecken
OVERLAY = "Coastlines_15m"
OUT_DIR = Path("sat")
ARCHIVE_DIR = OUT_DIR / "archive"
ARCHIVE_KEEP = 10               # juengste Staende je Platz
CHIP_PX = 768                   # Pixel je Teilbild (~320 m/px bei 245-km-Box)
HALF_LAT = 1.1                  # halbe Boxhoehe in Grad (~122 km)
BLANK_MEAN = 10.0               # mittlere Helligkeit darunter = leeres Komposit
                                 # (robust gegen Kuestenlinien-Overlay auf Schwarz)
MAX_DAYS_BACK = 2

MAIN_AIRPORTS = [
    ("CYFB", "Iqaluit",       63.756,  -68.556),
    ("CYIO", "Pond Inlet",    72.683,  -77.967),
    ("CYRB", "Resolute Bay",  74.717,  -94.969),
    ("CYHK", "Gjoa Haven",    68.636,  -95.850),
    ("CYCB", "Cambridge Bay", 69.108, -105.138),
    ("PABR", "Utqiagvik",     71.285, -156.766),
    ("PAOM", "Nome",          64.512, -165.445),
]

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def bbox_for(lat: float, lon: float) -> str:
    """BBOX minLat,minLon,maxLat,maxLon fuer ~250x250 km um den Platz."""
    half_lon = HALF_LAT / max(math.cos(math.radians(lat)), 0.15)
    return (f"{lat - HALF_LAT:.4f},{lon - half_lon:.4f},"
            f"{lat + HALF_LAT:.4f},{lon + half_lon:.4f}")


async def fetch_chip(client: httpx.AsyncClient, layer: str, bbox: str,
                     date: str, with_overlay: bool = True) -> Image.Image | None:
    layers = f"{layer},{OVERLAY}" if with_overlay else layer
    params = {"REQUEST": "GetSnapshot", "TIME": date, "BBOX": bbox,
              "CRS": "EPSG:4326", "LAYERS": layers,
              "FORMAT": "image/jpeg", "WIDTH": CHIP_PX, "HEIGHT": CHIP_PX}
    try:
        r = await client.get(SNAPSHOT_API, params=params, timeout=90.0)
        if r.status_code != 200:
            if with_overlay:            # Overlay-Layername ggf. geaendert
                return await fetch_chip(client, layer, bbox, date, False)
            print(f"  [HTTP {r.status_code}] {layer} {date}", file=sys.stderr)
            return None
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except (httpx.HTTPError, OSError) as exc:
        print(f"  [ERR] {layer} {date}: {exc}", file=sys.stderr)
        return None


def is_blank(img: Image.Image) -> bool:
    return ImageStat.Stat(img.convert("L")).mean[0] < BLANK_MEAN


async def chip_for_airport(client, icao, name, lat, lon,
                           date0: datetime) -> Path | None:
    bbox = bbox_for(lat, lon)
    panels: list[tuple[str, Image.Image, str]] = []
    for label, layer in LAYERS:
        img, used_date = None, None
        for back in range(MAX_DAYS_BACK + 1):
            d = (date0 - timedelta(days=back)).strftime("%Y-%m-%d")
            cand = await fetch_chip(client, layer, bbox, d)
            if cand is not None and not is_blank(cand):
                img, used_date = cand, d
                break
            if cand is not None and back == MAX_DAYS_BACK:
                img, used_date = cand, d           # lieber leer als nichts
        if img is None:
            return None
        panels.append((label, img, used_date))

    gap, cap_h = 6, 34
    w = CHIP_PX * 2 + gap
    sheet = Image.new("RGB", (w, CHIP_PX + cap_h), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    try:
        f_cap = ImageFont.truetype(FONT_BOLD, 16)
    except OSError:
        f_cap = ImageFont.load_default()

    for i, (label, img, used_date) in enumerate(panels):
        x0 = i * (CHIP_PX + gap)
        sheet.paste(img, (x0, cap_h))
        cx, cy = x0 + CHIP_PX // 2, cap_h + CHIP_PX // 2
        draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10],
                     outline=(255, 60, 60), width=3)
        draw.line([cx - 18, cy, cx - 10, cy], fill=(255, 60, 60), width=3)
        draw.line([cx + 10, cy, cx + 18, cy], fill=(255, 60, 60), width=3)
        stale = used_date != date0.strftime("%Y-%m-%d")
        draw.text((x0 + 6, 8), f"{label} — Bild vom {used_date}"
                  + (" (VORTAG!)" if stale else ""), font=f_cap,
                  fill=(255, 170, 60) if stale else (235, 235, 235))

    draw.text((w - 6, 8),
              f"{icao} {name} — VIIRS ~250x250 km — abgerufen "
              f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z",
              font=f_cap, fill=(235, 235, 235), anchor="ra")

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{icao}.jpg"
    sheet.save(out, quality=82)

    # Archiv: Zeitstempel-Kopie je Platz, die ARCHIVE_KEEP juengsten behalten
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    (ARCHIVE_DIR / f"{icao}_{stamp}.jpg").write_bytes(out.read_bytes())
    for p in sorted(ARCHIVE_DIR.glob(f"{icao}_*.jpg"))[:-ARCHIVE_KEEP]:
        p.unlink()
        print(f"  Archiv geloescht: {p.name}")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("airports", nargs="*", help="ICAO-Teilmenge (leer = alle)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (Default heute)")
    args = ap.parse_args()

    date0 = (datetime.strptime(args.date, "%Y-%m-%d")
             .replace(tzinfo=timezone.utc) if args.date
             else datetime.now(timezone.utc))
    wanted = {a.upper() for a in args.airports}
    targets = [a for a in MAIN_AIRPORTS if not wanted or a[0] in wanted]

    ok, fail = [], []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for icao, name, lat, lon in targets:
            out = await chip_for_airport(client, icao, name, lat, lon, date0)
            (ok if out else fail).append(icao)
            print(f"{icao}: {'OK -> ' + str(OUT_DIR / (icao + '.jpg'))
                            if out else 'FEHLGESCHLAGEN'}")
    if fail:
        print(f"Ohne Bild: {', '.join(fail)}", file=sys.stderr)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
