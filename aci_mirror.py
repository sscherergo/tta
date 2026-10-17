#!/usr/bin/env python3
"""aci_mirror.py — Arktis-Komposit (SSEC/AMRDC) als Korridor-Film.

Holt stuendlich das xlarge-Komposit (VIS + IR), schneidet den Bereich
des Flugpfads aus und pflegt einen rollierenden 5-Bilder-Loop
(animiertes GIF) — Bewegung der letzten ~5 h auf einen Blick.

GEOMETRIE (2026-07-12 gegen die Kuestenlinien des Produkts gefittet):
Das Komposit ist polarstereografisch, Nordpol exakt in Bildmitte — und
SPIEGELVERKEHRT: oestliche Laengen laufen GEGEN den Uhrzeigersinn
(AZ_SIGN = -1). Das ist der Kern des alten Fehlers.
  * ORIENT_UP_LON = +10.1, AZ_SIGN = -1.
    Kontrolle: 12 Kuesten-Landmarken in allen Quadranten (Kap Farvel,
    Nordkap, Utqiagvik, Nome, Reykjanes, Svalbard, Kap Tscheljuskin,
    Ostkap, Resolute, Iqaluit, Nowaja Semlja, Wrangel) liegen im Mittel
    1.6 px (~6 km) von der eingezeichneten Kueste entfernt. Das alte
    Modell (UP=-10, SIGN=+1) lag bei 32.8 px (~134 km).
  * Breitenkreise 80/70/60/50 N bei r = 260.3 / 524.6 / 797.2 / 1082.9 px
    (2048er Bild). Kreisfit: Mittelpunkt (1023, 1023),
    r = 2975.2 * tan((90-lat)/2), Streuung <0.1 % ueber alle vier Ringe.
  * Daraus EDGE_LAT: r(Bildkante) = 1024 -> lat = 52.011, NICHT 50.
    (Der 50-N-Kreis liegt knapp ausserhalb der Bildkante.)

Zwei Sackgassen, die nicht wiederholt werden duerfen:
  1. Die alte Selbst-Kalibrierung ueber rote Kuestenpixel: ihr Grob-Score
     war gesaettigt (Fangfenster ~45 px, binaerer Treffer -> praktisch
     jede Orientierung erreichte 14/14), also gewann immer der erste
     Rasterpunkt (-180 Grad). Ergebnis: um 180 Grad gedrehter Ausschnitt.
  2. Ein Abgleich gegen das cyane Gitter des Produkts ist WERTLOS: ein
     30-Grad-Meridianraster ist rotations- und spiegelsymmetrisch und
     bestaetigt jede falsche Orientierung mit ~98 %. Nur Kuestenlinien
     brechen diese Symmetrie.
verify_geometry() prueft deshalb Landmarken gegen die roten Kuestenpixel.

Ausgaben (Orphan-Branch `aci`):
  aci_vis_movie.gif   5-Frame-Loop VIS, Korridor-Ausschnitt
  aci_vis_latest.png  juengster VIS-Ausschnitt (Standbild, PDF)
  aci_ir_latest.png   juengster IR-Ausschnitt
  frames/vis_*.png    rollierender Framebestand (max 5)
  aci_debug.png       Kontrollbild: die MAGENTA Landmarken muessen auf der
                      roten Kueste sitzen (das gelbe Gitter beweist nichts,
                      siehe Sackgasse 2), Korridorrahmen rot

Test ohne Netz:
  python aci_mirror.py --local arcticcomposite-vis-xlarge.gif
"""
from __future__ import annotations

import argparse
import hashlib
import io
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

# httpx wird erst im Netzpfad importiert — so laeuft --local (Offline-Test
# des Zuschnitts) auch dort, wo httpx nicht installiert ist.

AMRDC = ("https://amrdc.ssec.wisc.edu/web_products/"
         "satellite_imagery/arctic/")
SRC = {"vis": "arcticcomposite-vis-xlarge.gif",
       "ir":  "arcticcomposite-ir-xlarge.gif"}
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition aci-mirror; "
      "+https://github.com/sscherergo/tta)"}
TIMEOUT_S = 90.0
MIN_BYTES = 100_000

# --- Gefittete Geometrie (siehe Modul-Docstring) ----------------------------
EDGE_LAT = 52.011          # Breite am Bildrand (Mitte der Kante)
ORIENT_UP_LON = 10.1       # Meridian, der im Bild nach oben zeigt
AZ_SIGN = -1               # -1 = oestliche Laengen GEGEN den Uhrzeigersinn

MARGIN_PX = 70             # Rand um den Korridor-Rahmen
FRAMES_KEEP = 5
FRAME_DIR = Path("frames")

CORRIDOR = [                            # Hauptplaetze der Route
    (63.756, -68.556), (72.683, -77.967), (74.717, -94.969),
    (68.636, -95.850), (69.108, -105.138), (71.285, -156.766),
    (64.512, -165.445),
]

# Kuesten-Landmarken zur Kontrolle — bewusst in ALLEN Quadranten, damit
# eine Drehung oder Spiegelung sofort auffaellt.
LANDMARKS = [
    ("Kap Farvel", 59.8, -43.9), ("Nordkap", 71.17, 25.78),
    ("Utqiagvik", 71.29, -156.77), ("Nome", 64.51, -165.45),
    ("Reykjanes", 63.8, -22.7), ("Svalbard Sued", 76.5, 16.6),
    ("Kap Tscheljuskin", 77.7, 104.3), ("Ostkap", 66.16, -169.8),
    ("Resolute", 74.72, -94.97), ("Iqaluit", 63.76, -68.56),
    ("Nowaja Semlja N", 76.9, 68.5), ("Wrangel", 71.2, -179.5),
]
LM_MAX_PX = 12.0                        # Mittel darueber = Geometrie kaputt
LM_SEARCH = 40                          # Suchradius um den Landmarkenpunkt

GRID_LATS = (50, 60, 70, 80)            # nur fuer das Kontrollbild
GRID_LONS = range(-180, 180, 30)


def ll_to_px(lat: float, lon: float, w: int, h: int) -> tuple[float, float]:
    """Polstereografisch: Pol = Bildmitte, EDGE_LAT am Rand,
    ORIENT_UP_LON zeigt nach oben. r ~ tan((90-lat)/2)."""
    cx, cy = (w - 1) / 2, (h - 1) / 2
    r_edge = min(cx, cy)
    r = r_edge * (math.tan(math.radians(90 - lat) / 2)
                  / math.tan(math.radians(90 - EDGE_LAT) / 2))
    az = math.radians((lon - ORIENT_UP_LON) * AZ_SIGN)
    return cx + r * math.sin(az), cy - r * math.cos(az)


def _is_red(px) -> bool:
    r, g, b = px[:3]
    return r > 200 and g < 80 and b < 80


def verify_geometry(img: Image.Image) -> float:
    """Mittlerer Abstand der Landmarken zur eingezeichneten Kuestenlinie.
    Kein Fit, kein Raten — reine Kontrolle. Dreht oder spiegelt AMRDC das
    Produkt, springt der Wert von ~2 px auf >30 px und der Lauf warnt."""
    w, h = img.size
    px = img.load()

    def dist_to_coast(x: float, y: float) -> float:
        xi, yi = int(round(x)), int(round(y))
        for r in range(LM_SEARCH + 1):          # wachsende Quadratringe
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue                # nur der neu hinzugekommene
                    xx, yy = xi + dx, yi + dy
                    if 0 <= xx < w and 0 <= yy < h and _is_red(px[xx, yy]):
                        return math.hypot(dx, dy)
        return float(LM_SEARCH)

    ds = [dist_to_coast(*ll_to_px(lat, lon, w, h))
          for _n, lat, lon in LANDMARKS]
    mean = sum(ds) / len(ds)
    verdict = "OK" if mean <= LM_MAX_PX else "FEHLGESCHLAGEN"
    print(f"[aci] Geometrie-Check: {len(LANDMARKS)} Landmarken, mittlerer "
          f"Abstand zur Kueste {mean:.1f} px — {verdict}")
    if mean > LM_MAX_PX:
        worst = sorted(zip(ds, [n for n, *_ in LANDMARKS]), reverse=True)[:3]
        print("[aci] WARNUNG: Projektion passt nicht! Ausschnitt ist NICHT "
              "vertrauenswuerdig. Schlechteste: "
              + ", ".join(f"{n} {d:.0f}px" for d, n in worst)
              + " — aci_debug.png pruefen.", file=sys.stderr)
    return mean


def stamp_strip(img: Image.Image) -> Image.Image | None:
    """Die Bildunterschrift des Produkts ("ARCTIC COMPOSITE VISIBLE IMAGE
    12 JUL 26 AT 17 UTC ...") liegt als schwarzer Streifen am unteren Rand,
    weit UNTERHALB des Korridor-Ausschnitts — im Crop fehlte sie deshalb.
    Hier wird sie ausgeschnitten und spaeter unter den Crop gesetzt: der
    Zeitstempel stammt damit vom Produkt selbst, nicht von unserer Uhr.

    Der Streifen wird gesucht, nicht hartkodiert: von unten alle Zeilen mit
    schwarzem Hintergrund (Median < 15), darin die breiteste Textgruppe."""
    w, h = img.size
    g = img.convert("L")
    px = g.load()

    def row_dark(y: int) -> bool:
        vals = sorted(px[x, y] for x in range(0, w, 8))
        return vals[len(vals) // 2] < 15

    y0 = h
    while y0 > h - 60 and row_dark(y0 - 1):
        y0 -= 1
    if y0 >= h - 3:                      # kein Streifen gefunden
        return None

    cols = [x for x in range(w)
            if any(px[x, y] > 100 for y in range(y0, h))]
    if not cols:
        return None
    groups, cur = [], [cols[0]]          # Luecken > 30 px trennen Gruppen
    for x in cols[1:]:
        if x - cur[-1] > 30:
            groups.append(cur)
            cur = [x]
        else:
            cur.append(x)
    groups.append(cur)
    main = max(groups, key=lambda gr: gr[-1] - gr[0])   # die Hauptzeile
    return img.crop((max(0, main[0] - 6), y0,
                     min(w, main[-1] + 7), h))


def with_stamp(crop: Image.Image, strip: Image.Image | None,
               fetched: str) -> Image.Image:
    """Crop + Zeitstempelzeile des Produkts + eigene Abrufzeit."""
    bar_h = 22
    if strip is None:                    # Notfall: wenigstens die Abrufzeit
        out = Image.new("RGB", (crop.width, crop.height + bar_h), (0, 0, 0))
        out.paste(crop, (0, 0))
        ImageDraw.Draw(out).text((6, crop.height + 4),
                                 f"AMRC Arctic Composite — geholt {fetched}",
                                 fill=(235, 235, 235))
        return out
    s = strip
    if s.width > crop.width:             # nur verkleinern, nie hochskalieren
        s = s.resize((crop.width,
                      max(1, round(s.height * crop.width / s.width))),
                     Image.LANCZOS)
    out = Image.new("RGB", (crop.width, crop.height + s.height + bar_h),
                    (0, 0, 0))
    out.paste(crop, (0, 0))
    out.paste(s, (0, crop.height))
    ImageDraw.Draw(out).text((6, crop.height + s.height + 4),
                             f"geholt {fetched}", fill=(160, 168, 178))
    return out


def corridor_box(w: int, h: int) -> tuple[int, int, int, int]:
    pts = [ll_to_px(lat, lon, w, h) for lat, lon in CORRIDOR]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(0, int(min(xs)) - MARGIN_PX),
            max(0, int(min(ys)) - MARGIN_PX),
            min(w, int(max(xs)) + MARGIN_PX),
            min(h, int(max(ys)) + MARGIN_PX))


def debug_image(img: Image.Image, box) -> None:
    """Kontrollbild. Entscheidend sind die MAGENTA Landmarken: die muessen
    auf der roten Kueste sitzen. Das gelbe Gitter ist nur Dekoration — es
    beweist nichts (siehe Docstring, Sackgasse 2)."""
    w, h = img.size
    dbg = img.copy()
    d = ImageDraw.Draw(dbg)
    for lat in GRID_LATS:
        pts = [ll_to_px(lat, lo, w, h) for lo in range(0, 361, 2)]
        d.line(pts, fill=(255, 255, 0), width=2)
    for lon in GRID_LONS:
        d.line([ll_to_px(EDGE_LAT, lon, w, h),
                ll_to_px(89.5, lon, w, h)], fill=(255, 255, 0), width=2)
    for _n, lat, lon in LANDMARKS:              # Beweis-Marker
        x, y = ll_to_px(lat, lon, w, h)
        d.ellipse([x - 14, y - 14, x + 14, y + 14],
                  outline=(255, 0, 255), width=5)
    d.rectangle(box, outline=(255, 60, 60), width=8)
    for lat, lon in CORRIDOR:
        x, y = ll_to_px(lat, lon, w, h)
        d.ellipse([x - 12, y - 12, x + 12, y + 12],
                  outline=(0, 230, 90), width=5)
    dbg.thumbnail((1100, 1100))
    dbg.save("aci_debug.png", optimize=True)


def fetch(client, fname: str) -> bytes | None:
    import httpx
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="lokale GIF-Datei statt Abruf (Test)")
    args = ap.parse_args()

    FRAME_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    fetched = datetime.now(timezone.utc).strftime("%d.%m. %H:%MZ")
    if args.local:
        raw = {"vis": Path(args.local).read_bytes(), "ir": None}
    else:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S),
                          follow_redirects=True) as client:
            raw = {k: fetch(client, f) for k, f in SRC.items()}
    if raw["vis"] is None and raw["ir"] is None:
        print("ACI: keine Quelle lieferbar — Abbruch")
        sys.exit(1)

    for key, data in raw.items():
        if data is None:
            continue
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        verify_geometry(img)                    # je Kanal — IR kann abweichen
        box = corridor_box(w, h)
        crop = img.crop(box)
        strip = stamp_strip(img)                # Zeitstempelzeile des Produkts
        if strip is None:
            print("[aci] WARNUNG: Bildunterschrift nicht gefunden — "
                  "Ausschnitt ohne Produkt-Zeitstempel!", file=sys.stderr)
        stamped = with_stamp(crop, strip, fetched)
        stamped.save(f"aci_{key}_latest.png", optimize=True)
        print(f"[aci] {key}: Quelle {w}x{h}, Ausschnitt "
              f"{crop.size[0]}x{crop.size[1]} (Box {box})"
              + (f" + Zeitstempelzeile {strip.size[0]}x{strip.size[1]}"
                 if strip else " OHNE Zeitstempel"))

        if key == "vis":
            # Aenderungserkennung auf dem ROHEN Crop — der aufgepraegte
            # Zeitstempel aendert sich sonst jede Stunde und jeder Lauf
            # erzeugte einen "neuen" Frame, auch ohne neues Satellitenbild.
            digest = hashlib.sha256(crop.tobytes()).hexdigest()
            sha_file = FRAME_DIR / "last.sha256"
            last_digest = (sha_file.read_text().strip()
                           if sha_file.exists() else "")
            frames = sorted(FRAME_DIR.glob("vis_*.png"))
            if digest != last_digest:
                stamped.save(FRAME_DIR / f"vis_{now}.png", optimize=True)
                sha_file.write_text(digest)
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

            debug_image(img, box)

    print("ACI: fertig.")


if __name__ == "__main__":
    main()
