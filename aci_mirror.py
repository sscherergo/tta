#!/usr/bin/env python3
"""aci_mirror.py — Arktis-Komposit (SSEC/AMRDC) als Korridor-Film.

Holt stuendlich das xlarge-Komposit (VIS + IR), schneidet den Bereich
des Flugpfads aus und pflegt einen rollierenden 5-Bilder-Loop
(animiertes GIF) — Bewegung der letzten ~5 h auf einen Blick.

Geometrie-Annahme (Produktspez.: Polstereografie, 50-90N):
Pol = Bildmitte, 50N = Bildrand; ORIENT_UP_LON = Meridian, der im
Bild nach oben zeigt. Kalibrierung: aci_debug.png zeigt die
Vollscheibe mit eingezeichnetem Korridor-Rahmen — passt der Rahmen
nicht auf die Geografie, ORIENT_UP_LON anpassen (eine Zahl).

Ausgaben (Orphan-Branch `aci`):
  aci_vis_movie.gif   5-Frame-Loop VIS, Korridor-Ausschnitt
  aci_vis_latest.png  juengster VIS-Ausschnitt (Standbild, PDF)
  aci_ir_latest.png   juengster IR-Ausschnitt
  frames/vis_*.png    rollierender Framebestand (max 5)
  aci_debug.png       Kalibrier-Vollscheibe mit Korridor-Rahmen
"""
from __future__ import annotations

import hashlib
import io
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

AMRDC = ("https://amrdc.ssec.wisc.edu/web_products/"
         "satellite_imagery/arctic/")
SRC = {"vis": "arcticcomposite-vis-xlarge.gif",
       "ir":  "arcticcomposite-ir-xlarge.gif"}
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition aci-mirror; "
      "+https://github.com/sscherergo/tta)"}
TIMEOUT = httpx.Timeout(90.0)
MIN_BYTES = 100_000

EDGE_LAT = 50.0                 # Breitenkreis am Bildrand (Produktspez.)
ORIENT_UP_LON = 0.0             # Greenwich oben ...
AZ_SIGN = -1                    # kalibriert 12.07.: Ost-West GESPIEGELT
                                 # (Belege: Groenland oben-rechts, Sibirien
                                 # links, Nachtzone 20Z ueber Russland)
MARGIN_PX = 70                  # Rand um den Korridor-Rahmen
FRAMES_KEEP = 5
FRAME_DIR = Path("frames")

CORRIDOR = [                    # Hauptplaetze der Route
    (63.756, -68.556), (72.683, -77.967), (74.717, -94.969),
    (68.636, -95.850), (69.108, -105.138), (71.285, -156.766),
    (64.512, -165.445),
]


def ll_to_px(lat: float, lon: float, w: int, h: int) -> tuple[float, float]:
    """Polstereografisch: Pol=Mitte, EDGE_LAT=Rand, ORIENT_UP_LON oben."""
    cx, cy = w / 2, h / 2
    r_edge = min(cx, cy)
    r = r_edge * (math.tan(math.radians(90 - lat) / 2)
                  / math.tan(math.radians(90 - EDGE_LAT) / 2))
    az = math.radians((lon - ORIENT_UP_LON) * AZ_SIGN)
    return cx + r * math.sin(az), cy - r * math.cos(az)


def corridor_box(w: int, h: int) -> tuple[int, int, int, int]:
    pts = [ll_to_px(lat, lon, w, h) for lat, lon in CORRIDOR]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(0, int(min(xs)) - MARGIN_PX),
            max(0, int(min(ys)) - MARGIN_PX),
            min(w, int(max(xs)) + MARGIN_PX),
            min(h, int(max(ys)) + MARGIN_PX))


def fetch(client: httpx.Client, fname: str) -> bytes | None:
    try:
        r = client.get(AMRDC + fname, headers=UA)
    except httpx.HTTPError as e:
        print(f"[aci] {fname}: {e}")
        return None
    if r.status_code != 200 or len(r.content) < MIN_BYTES:
        print(f"[aci] {fname}: HTTP {r.status_code}, {len(r.content)} B")
        return None
    return r.content


def main() -> None:
    FRAME_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        raw = {k: fetch(client, f) for k, f in SRC.items()}
    if raw["vis"] is None and raw["ir"] is None:
        print("ACI: keine Quelle lieferbar — Abbruch")
        sys.exit(1)

    for key, data in raw.items():
        if data is None:
            continue
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        box = corridor_box(w, h)
        crop = img.crop(box)
        crop.save(f"aci_{key}_latest.png", optimize=True)
        print(f"[aci] {key}: Quelle {w}x{h}, Ausschnitt "
              f"{crop.size[0]}x{crop.size[1]} (Box {box})")

        if key == "vis":
            digest = hashlib.sha256(crop.tobytes()).hexdigest()
            frames = sorted(FRAME_DIR.glob("vis_*.png"))
            last_digest = ""
            if frames:
                last = Image.open(frames[-1]).convert("RGB")
                if last.size == crop.size:
                    last_digest = hashlib.sha256(last.tobytes()).hexdigest()
            if digest != last_digest:
                crop.save(FRAME_DIR / f"vis_{now}.png", optimize=True)
                frames = sorted(FRAME_DIR.glob("vis_*.png"))
                for old in frames[:-FRAMES_KEEP]:
                    old.unlink()
                frames = sorted(FRAME_DIR.glob("vis_*.png"))
                print(f"[aci] neuer Frame vis_{now} — Bestand {len(frames)}")
            else:
                print("[aci] vis unveraendert — kein neuer Frame")
            seq = [Image.open(f).convert("RGB") for f in frames]
            if seq:
                base = seq[-1].size
                seq = [f if f.size == base else f.resize(base) for f in seq]
                dur = ([700] * (len(seq) - 1) + [1600]) if len(seq) > 1 \
                    else [1600]
                seq[0].save("aci_vis_movie.gif", save_all=True,
                            append_images=seq[1:], duration=dur, loop=0)
                print(f"[aci] Movie: {len(seq)} Frames -> aci_vis_movie.gif")

            dbg = img.copy()
            d = ImageDraw.Draw(dbg)
            d.rectangle(box, outline=(255, 60, 60), width=6)
            for lat, lon in CORRIDOR:
                x, y = ll_to_px(lat, lon, w, h)
                d.ellipse([x - 8, y - 8, x + 8, y + 8],
                          outline=(0, 230, 90), width=4)
            dbg.thumbnail((1100, 1100))
            dbg.save("aci_debug.png", optimize=True)

    print("ACI: fertig.")


if __name__ == "__main__":
    main()
