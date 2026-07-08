"""
sat_movie.py — v4 (aktuelles Bild + Live-Zoom, kein Archiv)
===========================================================

Aktuelles Ueberflug-Mosaik ueber den Flugkorridor auf Basis einzelner
VIIRS-Granulen (GIBS, 6-min-Raster, EPSG:3413). Es wird nur EIN Bild
gepflegt (sat_movie/latest.jpg, ueberschrieben) plus meta.json mit den
Sektor-Bounding-Boxes und den exakten Ueberflugzeiten — der Viewer
(movie.html) nutzt die Metadaten fuer den Live-Zoom: Detailausschnitte
in ~240 m/px werden beim Zoomen direkt von GIBS geladen, nicht im Repo
gespeichert. Jeder Sektor traegt Tag+UTC seiner Ueberfluege im Bild.

Committet wird nur bei sichtbarer Aenderung (neue Ueberfluege).
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageFont,
                 ImageStat)

WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3413/best/wms.cgi"
LAYERS = [
    ("True Color", "VIIRS_NOAA20_CorrectedReflectance_TrueColor_Granule"),
    ("Snow/Fog-RGB (weiss=Nebel/Wasserwolke, tuerkis=Eis)",
     "VIIRS_NOAA20_CorrectedReflectance_BandsM3-I3-M11_Granule"),
]
PROBE_LAYER = LAYERS[0][1]
OVERLAY = "Coastlines_15m"

LAT_MIN, LAT_MAX = 62.0, 77.0
SECTORS = [("West", -168.0, -133.0), ("Zentral", -133.0, -98.0),
           ("Ost", -98.0, -63.0)]
PW = 800                        # Panelbreite je Sektor (Uebersicht ~2.4 km/px)
GAP = 4
CAP = 34
OUT_DIR = Path("sat_movie")
LATEST = OUT_DIR / "latest.jpg"
META = OUT_DIR / "meta.json"
SLOT_MIN = 6
MAX_SLOTS = 80                  # 80 x 6 min = 8 h Suchtiefe (Orbit + Latenz)
TARGET_PER_SECTOR = 3           # Granulen je Sektor (2-3 Ueberfluege)
GRANULE_MEAN = 2.0
DIFF_MEAN = 3.0
LATENCY_MIN = 80                # LANCE-Latenz: Suche beginnt frueher
DAILY_LAYERS = [
    ("True Color", "VIIRS_NOAA20_CorrectedReflectance_TrueColor"),
    ("Snow/Fog-RGB (weiss=Nebel/Wasserwolke, tuerkis=Eis)",
     "VIIRS_NOAA20_CorrectedReflectance_BandsM3-I3-M11"),
]
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# --- EPSG:3413 (Polar Stereographic North, lat_ts=70, lon0=-45, WGS84) -----
_A, _E = 6378137.0, 0.0818191908426
_LON0 = math.radians(-45.0)

def _t(phi: float) -> float:
    es = _E * math.sin(phi)
    return math.tan(math.pi / 4 - phi / 2) / ((1 - es) / (1 + es)) ** (_E / 2)

_M70 = math.cos(math.radians(70)) / math.sqrt(
    1 - _E ** 2 * math.sin(math.radians(70)) ** 2)
_T70 = _t(math.radians(70))

def project(lat: float, lon: float) -> tuple[float, float]:
    rho = _A * _M70 * _t(math.radians(lat)) / _T70
    ang = math.radians(lon) - _LON0
    return rho * math.sin(ang), -rho * math.cos(ang)


def sector_bbox(lon_a: float, lon_b: float) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for k in range(25):
        lon = lon_a + k * (lon_b - lon_a) / 24
        for lat in (LAT_MIN, LAT_MAX):
            x, y = project(lat, lon)
            xs.append(x); ys.append(y)
    for k in range(13):
        lat = LAT_MIN + k * (LAT_MAX - LAT_MIN) / 12
        for lon in (lon_a, lon_b):
            x, y = project(lat, lon)
            xs.append(x); ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)

SECTOR_BOX = {name: sector_bbox(a, b) for name, a, b in SECTORS}
SECTOR_H = {name: round(PW * (bb[3] - bb[1]) / (bb[2] - bb[0]))
            for name, bb in SECTOR_BOX.items()}
PH = max(SECTOR_H.values())     # gemeinsame Zeilenhoehe
FRAME_W = PW * len(SECTORS) + GAP * (len(SECTORS) - 1)


# --- WMS-Abruf ---------------------------------------------------------------

async def fetch_wms(client, layers: str, time: str | None, bbox, w: int,
                    h: int, fmt="image/jpeg",
                    transparent=False) -> Image.Image | None:
    params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
              "LAYERS": layers, "STYLES": "", "SRS": "EPSG:3413",
              "BBOX": f"{bbox[0]:.0f},{bbox[1]:.0f},{bbox[2]:.0f},{bbox[3]:.0f}",
              "WIDTH": w, "HEIGHT": h, "FORMAT": fmt}
    if time:
        params["TIME"] = time
    if transparent:
        params["TRANSPARENT"] = "TRUE"
    try:
        r = await client.get(WMS, params=params, timeout=120.0)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image"):
            body = r.text[:200].replace("\n", " ") if hasattr(r, "text") else ""
            print(f"[WMS {r.status_code} {ctype}] {layers} {time}: {body}",
                  file=sys.stderr)
            return None
        return Image.open(io.BytesIO(r.content))
    except (httpx.HTTPError, OSError) as exc:
        print(f"[ERR] {layers} {time}: {exc}", file=sys.stderr)
        return None


def _mean(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0]


async def find_granules(client) -> dict[str, list[str]]:
    """Je Sektor die juengsten Slots mit Ueberflug (kleine Probebilder).
    Die Sektor-Proben eines Slots laufen parallel; je Sektor endet die
    Suche bei TARGET_PER_SECTOR Treffern."""
    t0 = datetime.now(timezone.utc) - timedelta(minutes=LATENCY_MIN)
    t0 = t0.replace(minute=(t0.minute // SLOT_MIN) * SLOT_MIN,
                    second=0, microsecond=0)
    hits: dict[str, list[str]] = {name: [] for name, *_ in SECTORS}

    async def probe(name: str, stamp: str) -> tuple[str, str, bool]:
        bb = SECTOR_BOX[name]
        img = await fetch_wms(client, PROBE_LAYER, stamp, bb, 160,
                              max(1, round(160 * (bb[3]-bb[1])
                                           / (bb[2]-bb[0]))))
        ok = img is not None and _mean(img.convert("RGB")) >= GRANULE_MEAN
        return name, stamp, ok

    for k in range(MAX_SLOTS):
        lacking = [n for n, v in hits.items() if len(v) < TARGET_PER_SECTOR]
        if not lacking:
            break
        stamp = (t0 - timedelta(minutes=SLOT_MIN * k)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        for name, s, ok in await asyncio.gather(
                *(probe(n, stamp) for n in lacking)):
            if ok:
                hits[name].append(s)
    return hits


EDGE_GRANULE = (255, 160, 0)     # orange: Grenze aktueller Ueberflug-Daten
EDGE_TODAY = (145, 145, 145)     # grau: Grenze heutiges Komposit (aussen: Vortag)


def coverage_paste(base: Image.Image, img: Image.Image) -> Image.Image:
    """img dort einfuegen, wo es Daten hat (Abdeckungsmaske statt
    Helligkeit): Neuestes liegt oben, auch wenn es dunkler ist.
    Rueckgabe: die Abdeckungsmaske (fuer Schichtgrenzen)."""
    mask = img.convert("L").point(lambda v: 255 if v > 3 else 0)
    # Nadelloecher schliessen (dunkle JPEG-Pixel im Bildinneren wuerden
    # sonst als kleine orange Rahmen-Artefakte konturiert), dann Rand
    # gegen JPEG-Kantenrauschen leicht schrumpfen:
    mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    mask = mask.filter(ImageFilter.MinFilter(3))
    base.paste(img, (0, 0), mask)
    return mask


def mask_edge(mask: Image.Image, px: int = 2) -> Image.Image:
    """Innenkontur einer Abdeckungsmaske (px Pixel breit)."""
    eroded = mask.filter(ImageFilter.MinFilter(2 * px + 1))
    return ImageChops.subtract(mask, eroded)


def draw_edge(mosaic: Image.Image, edge: Image.Image, color) -> None:
    mosaic.paste(Image.new("RGB", mosaic.size, color), (0, 0), edge)


async def build_sector(client, idx: int, name: str, stamps: list[str],
                       daily_dates: list[str]) -> Image.Image:
    """Panel: Tageskomposite (alt -> neu) als Basis, Granulen obenauf.
    Schichtgrenzen werden als Konturen markiert, damit alte und frische
    Wolken-/Nebelkanten nicht verwechselt werden."""
    bb = SECTOR_BOX[name]
    h = SECTOR_H[name]
    mosaic = Image.new("RGB", (PW, h), (0, 0, 0))
    m_today = Image.new("L", (PW, h), 0)
    m_gran = Image.new("L", (PW, h), 0)
    for k, d in enumerate(daily_dates):            # gestern, dann heute
        img = await fetch_wms(client, DAILY_LAYERS[idx][1], d, bb, PW, h)
        if img is not None:
            m = coverage_paste(mosaic, img.convert("RGB"))
            if k == len(daily_dates) - 1:          # heutiges Komposit
                m_today = m
    for stamp in sorted(stamps):                   # Ueberfluege, neu oben
        img = await fetch_wms(client, LAYERS[idx][1], stamp, bb, PW, h)
        if img is not None:
            m = coverage_paste(mosaic, img.convert("RGB"))
            m_gran = ImageChops.lighter(m_gran, m)

    # Schichtgrenzen: grau = heutiges Komposit (nur ausserhalb der
    # Ueberflug-Flaeche relevant), orange = aktuelle Ueberflug-Daten
    inv_gran = ImageChops.invert(m_gran)
    edge_today = ImageChops.multiply(mask_edge(m_today), inv_gran)
    draw_edge(mosaic, edge_today, EDGE_TODAY)
    draw_edge(mosaic, mask_edge(m_gran), EDGE_GRANULE)

    coast = await fetch_wms(client, OVERLAY, None, bb, PW, h,
                            fmt="image/png", transparent=True)
    if coast is not None:
        mosaic.paste(coast, (0, 0), coast.convert("RGBA"))
    return mosaic


def changed_vs_latest(row: Image.Image) -> bool:
    if not LATEST.exists():
        return True
    try:
        prev = Image.open(LATEST).convert("RGB")
    except OSError:
        return True
    a = row.resize((128, 48))
    b = prev.crop((0, CAP, FRAME_W, CAP + PH)).resize((128, 48))
    diff = _mean(ImageChops.difference(a, b))
    print(f"Differenz zum aktuellen Bild: {diff:.1f}")
    return diff >= DIFF_MEAN


async def main() -> None:
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        hits = await find_granules(client)
        print("Granulen je Sektor: " + ", ".join(
            f"{k}={len(v)}" for k, v in hits.items()))

        # Basis fuer ALLE Sektoren: Komposite gestern + heute (alt -> neu);
        # Granulen-Sektoren bekommen ihre Ueberfluege obenauf.
        daily_dates = [(now - timedelta(days=1)).strftime("%Y-%m-%d"),
                       now.strftime("%Y-%m-%d")]
        sector_info: dict[str, dict] = {}
        for name, *_r in SECTORS:
            if hits[name]:
                sector_info[name] = {"mode": "granule",
                                     "times": sorted(hits[name])}
            else:
                sector_info[name] = {"mode": "daily",
                                     "times": [daily_dates[-1]]}
                print(f"Sektor {name}: kein Ueberflug im Fenster — "
                      f"Basis Tageskomposit")

        rows: list[tuple[str, Image.Image]] = []
        for idx, (label, _lyr) in enumerate(LAYERS):
            row = Image.new("RGB", (FRAME_W, PH), (12, 12, 12))
            x0 = 0
            for name, *_r in SECTORS:
                info = sector_info[name]
                stamps = info["times"] if info["mode"] == "granule" else []
                panel = await build_sector(client, idx, name, stamps,
                                           daily_dates)
                row.paste(panel, (x0, (PH - panel.height) // 2))
                x0 += PW + GAP
            rows.append((label, row))

    if not changed_vs_latest(rows[0][1]):
        print("Keine sichtbare Aenderung — kein neuer Frame.")
        return

    sheet = Image.new("RGB", (FRAME_W, (PH + CAP) * 2), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    try:
        f = ImageFont.truetype(FONT_BOLD, 16)
        f_small = ImageFont.truetype(FONT_BOLD, 14)
    except OSError:
        f = f_small = ImageFont.load_default()

    gran_stamps = sorted(set(
        s for v in sector_info.values() if v["mode"] == "granule"
        for s in v["times"]))
    n_gran = len(gran_stamps)
    daily_sectors = [n for n, v in sector_info.items() if v["mode"] == "daily"]
    if gran_stamps:
        span = gran_stamps[0][11:16] + "-" + gran_stamps[-1][11:16] + "Z"
        title = f"VIIRS NOAA-20 Ueberfluege {span} ({n_gran} Granulen)"
        if daily_sectors:
            title += f" | {'/'.join(daily_sectors)}: Tageskomposit"
    else:
        title = "VIIRS NOAA-20 TAGESKOMPOSIT (keine Einzel-Ueberfluege)"
    title += f" — abgerufen {now:%Y-%m-%d %H:%M}Z"

    def times_label(name: str) -> str:
        info = sector_info[name]
        if info["mode"] == "daily":
            return (f"Tageskomposit bis {info['times'][0]} "
                    f"(kein Pass im Fenster)")
        by_day: dict[str, list[str]] = {}
        for s in info["times"]:
            day = f"{s[8:10]}.{s[5:7]}."
            by_day.setdefault(day, []).append(s[11:16] + "Z")
        return ("  ".join(d + " " + " · ".join(ts)
                          for d, ts in by_day.items())
                + "  (Basis: Komposit)")

    for i, (label, row) in enumerate(rows):
        y0 = i * (PH + CAP)
        draw.text((6, y0 + 8), label, font=f, fill=(235, 235, 235))
        sheet.paste(row, (0, y0 + CAP))
        # Ueberflugzeiten je Sektor, in beiden Zeilen unten links im Panel
        for j, (name, *_r) in enumerate(SECTORS):
            x0 = j * (PW + GAP)
            draw.text((x0 + 6, y0 + CAP + PH - 22),
                      f"{name}: {times_label(name)}", font=f_small,
                      fill=(255, 235, 140), stroke_width=2,
                      stroke_fill=(0, 0, 0))
    for j, (name, *_r) in enumerate(SECTORS):
        draw.text((j * (PW + GAP) + PW - 6, CAP + 6), name, font=f,
                  fill=(200, 200, 200), anchor="ra")
    draw.text((FRAME_W - 6, 8), title,
              font=f, fill=(235, 235, 235), anchor="ra")
    draw.text((FRAME_W // 2, CAP + 6),
              "Schichtgrenzen:  orange = aktuelle Ueberflug-Daten  |  "
              "grau = heutiges Komposit (aussen: Vortag)",
              font=f_small, fill=(220, 220, 220), anchor="ma",
              stroke_width=2, stroke_fill=(0, 0, 0))

    OUT_DIR.mkdir(exist_ok=True)
    for p in OUT_DIR.glob("region_*.jpg"):
        p.unlink()                          # Altbestand des Archiv-Modus
    (OUT_DIR / "manifest.json").unlink(missing_ok=True)
    sheet.save(LATEST, quality=80)
    overall = ("granule" if not daily_sectors
               else ("daily" if not gran_stamps else "hybrid"))
    META.write_text(json.dumps({
        "updated": now.isoformat(),
        "wms": WMS,
        "mode": overall,
        "frame": {"w": FRAME_W, "panel_w": PW, "panel_h": PH,
                  "gap": GAP, "cap": CAP},
        "layers": [{"label": lbl, "layer": lyr} for lbl, lyr in LAYERS],
        "sectors": [{"name": name,
                     "bbox": SECTOR_BOX[name],
                     "h": SECTOR_H[name],
                     "mode": sector_info[name]["mode"],
                     "granules": sector_info[name]["times"],
                     "zoom": {
                         "time": (sector_info[name]["times"][-1]
                                  if sector_info[name]["times"] else None),
                         "layers": [lyr for _l, lyr in
                                    (LAYERS if sector_info[name]["mode"]
                                     == "granule" else DAILY_LAYERS)]}}
                    for name, *_r in SECTORS],
    }, indent=1))
    print(f"Aktualisiert: {LATEST} ({LATEST.stat().st_size // 1024} KB) — "
          f"{title}")


if __name__ == "__main__":
    asyncio.run(main())
