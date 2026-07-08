"""
map_dashboard.py
================

Zeichnet das Block-0-Machbarkeits-Dashboard auf die Routenkarte
(TTP_Routing.jpg): Headwind-Komponente in der Mitte jedes Legs,
Zielplatz-Parameter (XW, CIG, SP, TRD, ICE) als Panel am Flughafen,
Gesamturteil als farbiger Ring um den Marker.
Farben: gruen = OK, orange = WARN, rot = NOGO.

Nutzung:
    python map_dashboard.py                  # juengstes Briefing, +12 h
    python map_dashboard.py --lead 24        # Zeile naechst +24 h
    python map_dashboard.py --map TTP_Routing.jpg --out dashboard_map.png

Datenquelle: neueste Datei in briefings/, sonst briefing.txt.
Abhaengigkeiten: pip install pillow   (PIL; Rest wie gehabt)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

MAIN_ORDER = ["CYFB", "CYIO", "CYRB", "CYHK", "CYCB", "PABR", "PAOM"]
EXTRA_AIRPORTS = ["CYEV"]                 # Inuvik: Alternate mit eigenem Panel
MAP_AIRPORTS = MAIN_ORDER + EXTRA_AIRPORTS
EXTRA_LEGS = [("CYCB", "CYEV")]           # HW-Label auch auf diesem Leg
ARCHIVE_DIR = Path("maps")
ARCHIVE_KEEP = 10

# Pixelkoordinaten der Marker-Spitzen in TTP_Routing.jpg (1897 x 862)
PIXELS: dict[str, tuple[int, int]] = {
    "CYFB": (1660, 552),
    "CYIO": (1340, 295),
    "CYRB": (1105, 283),
    "CYHK": (1147, 565),
    "CYCB": (992, 553),
    "PABR": (330, 184),
    "PAOM": (62, 272),
    "CYEV": (525, 494),
}

# Panel-Versatz je Platz (px), gewaehlt um Route/Labels nicht zu verdecken
PANEL_OFFSET: dict[str, tuple[int, int]] = {
    "CYFB": (18, 12),
    "CYIO": (18, -150),
    "CYRB": (-165, -150),
    "CYHK": (18, 18),
    "CYCB": (-160, 18),
    "PABR": (22, -40),
    "PAOM": (16, 20),
    "CYEV": (-40, 20),
}

COLOR = {"OK": (46, 204, 64), "WARN": (255, 153, 0), "NOGO": (255, 48, 48),
         "?": (170, 170, 170)}
PANEL_BG = (10, 10, 10, 175)
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# ---------------------------------------------------------------------------
# Briefing-Parsing (Block 0, alle 7 Spalten)
# ---------------------------------------------------------------------------

HDR_RE = re.compile(r"^--- ([A-Z]{4}) ")
ROW_RE = re.compile(r"^\s*(\d{2})\. (\d{2})Z\s+(.+?)\s+\[(OK|WARN|NOGO)\]\s*$")
TOKEN_RE = re.compile(
    r"(>5000|[+\-]?\d+(?:\.\d+)?|—|\?)(~?)(?:\s+(OK|WARN|NOGO))?")
STAMP_RE = re.compile(r"briefing_(\d{8})T(\d{4})Z\.txt$")
MADE_RE = re.compile(r"erzeugt (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC")
RUN_RE = re.compile(r"VORHERSAGE: CAPS-Modelllauf (\d{8}) (\d{2}):00 UTC")


def resolve_valid(made: datetime, day: int, hour: int) -> datetime | None:
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
    files = sorted(Path("briefings").glob("briefing_*.txt")) \
        if Path("briefings").exists() else []
    if files:
        path = files[-1]
        m = STAMP_RE.search(path.name)
        made = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M") \
            .replace(tzinfo=timezone.utc)
        return path.read_text(encoding="utf-8", errors="replace"), made
    path = Path("briefing.txt")
    if not path.exists():
        sys.exit("Kein Briefing gefunden (briefings/ oder briefing.txt).")
    text = path.read_text(encoding="utf-8", errors="replace")
    m = MADE_RE.search(text.splitlines()[0]) or MADE_RE.search(text)
    if not m:
        sys.exit("Erzeugungszeit im Briefing nicht gefunden.")
    made = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H:%M") \
        .replace(tzinfo=timezone.utc)
    return text, made


def parse_block0(text: str, made: datetime) -> list[dict]:
    rows, icao = [], None
    for line in text.splitlines():
        if line.startswith("BLOCK 1"):
            break
        h = HDR_RE.match(line.strip())
        if h:
            icao = h.group(1) if h.group(1) in MAP_AIRPORTS else None
            continue
        if icao is None:
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        toks = TOKEN_RE.findall(m.group(3))
        if len(toks) == 7:          # neues TRD-Format: Delta -> Projektion
            hw, xw, cig, sp, trd_d, trd_p, ice = toks
            trd = (trd_d[0], "", trd_p[2])   # Anzeige Delta, Klasse Projektion
        elif len(toks) == 6:
            hw, xw, cig, sp, trd, ice = toks
        else:
            continue
        valid = resolve_valid(made, int(m.group(1)), int(m.group(2)))
        if valid is None:
            continue
        rows.append({"icao": icao, "valid": valid, "total": m.group(4),
                     "HW": hw, "XW": xw, "CIG": cig, "SP": sp,
                     "TRD": trd, "ICE": ice})
    return rows


def pick_rows(rows: list[dict], target: datetime) -> dict[str, dict]:
    """Je Platz die Zeile mit valid am naechsten zum Zielzeitpunkt."""
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["icao"])
        if cur is None or (abs((r["valid"] - target).total_seconds())
                           < abs((cur["valid"] - target).total_seconds())):
            best[r["icao"]] = r
    return best


# ---------------------------------------------------------------------------
# Zeichnen
# ---------------------------------------------------------------------------

def tok_text(tok, unit="") -> str:
    val, tilde, _cls = tok
    if val in ("—", "?"):
        return val
    return f"{val}{tilde}{unit}"


def tok_color(tok):
    return COLOR.get(tok[2] or "?", COLOR["?"])


def draw_panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int], icao: str,
               row: dict, f_hdr, f_txt) -> None:
    lines = [("XW",  tok_text(row["XW"], "kt"),  tok_color(row["XW"])),
             ("CIG", tok_text(row["CIG"], "ft"), tok_color(row["CIG"])),
             ("SP",  tok_text(row["SP"], "°"),   tok_color(row["SP"])),
             ("TRD", tok_text(row["TRD"]),       tok_color(row["TRD"])),
             ("ICE", tok_text(row["ICE"], "°"),  tok_color(row["ICE"]))]
    pad, lh = 7, 19
    w = 128
    h = pad * 2 + lh * (len(lines) + 1)
    x, y = xy
    draw.rounded_rectangle([x, y, x + w, y + h], radius=7, fill=PANEL_BG,
                           outline=COLOR[row["total"]], width=3)
    draw.text((x + pad, y + pad), icao, font=f_hdr,
              fill=COLOR[row["total"]])
    for i, (label, val, col) in enumerate(lines, start=1):
        yy = y + pad + lh * i
        draw.text((x + pad, yy), label, font=f_txt, fill=(225, 225, 225))
        draw.text((x + pad + 42, yy), val, font=f_txt, fill=col)


def draw_map(map_path: Path, out_path: Path, picked: dict[str, dict],
             made: datetime, target: datetime,
             run_dt: datetime | None) -> None:
    base = Image.open(map_path).convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ov)
    try:
        f_hdr = ImageFont.truetype(FONT_BOLD, 17)
        f_txt = ImageFont.truetype(FONT_REG, 14)
        f_hw = ImageFont.truetype(FONT_BOLD, 18)
        f_title = ImageFont.truetype(FONT_BOLD, 20)
    except OSError:
        f_hdr = f_txt = f_hw = f_title = ImageFont.load_default()

    # Headwind in Leg-Mitte (HW-Wert steht in der Zeile des Zielplatzes)
    legs = list(zip(MAIN_ORDER[:-1], MAIN_ORDER[1:])) + EXTRA_LEGS
    for a, b in legs:
        row = picked.get(b)
        if row is None:
            continue
        xa, ya = PIXELS[a]
        xb, yb = PIXELS[b]
        mx, my = (xa + xb) // 2, (ya + yb) // 2
        txt = f"HW {tok_text(row['HW'], 'kt')}"
        col = tok_color(row["HW"])
        draw.text((mx, my), txt, font=f_hw, fill=col, anchor="mm",
                  stroke_width=3, stroke_fill=(0, 0, 0))

    # Zielplatz-Panels + Gesamt-Ring
    for icao in MAP_AIRPORTS:
        row = picked.get(icao)
        if row is None:
            continue
        x, y = PIXELS[icao]
        c = COLOR[row["total"]]
        draw.ellipse([x - 16, y - 16, x + 16, y + 16], outline=c, width=4)
        ox, oy = PANEL_OFFSET[icao]
        draw_panel(draw, (x + ox, y + oy), icao, row, f_hdr, f_txt)

    # Titelzeile
    valid = next(iter(picked.values()))["valid"]
    run_part = (f"Vorhersage: CAPS-Lauf {run_dt:%d.%m. %H:%M}Z  |  "
                if run_dt else "")
    title = (f"NWP-Dashboard  |  {run_part}"
             f"Briefing erzeugt {made:%d.%m. %H:%M}Z  |  "
             f"gueltig {valid:%d.%m. %H:00}Z  |  OK/WARN/NOGO")
    tw = draw.textlength(title, font=f_title)
    draw.rounded_rectangle([10, 10, 30 + tw, 44], radius=7, fill=PANEL_BG)
    draw.text((20, 16), title, font=f_title, fill=(240, 240, 240))

    Image.alpha_composite(base, ov).convert("RGB").save(out_path, quality=92)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=12,
                    help="Vorlaufzeit in h ab Briefing-Erzeugung (Default 12)")
    ap.add_argument("--map", default="TTP_Routing.jpg")
    ap.add_argument("--out", default="dashboard_map.png")
    args = ap.parse_args()

    text, made = load_latest_briefing()
    rows = parse_block0(text, made)
    if not rows:
        sys.exit("Keine Block-0-Zeilen im Briefing gefunden.")
    m = RUN_RE.search(text)
    run_dt = (datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H")
              .replace(tzinfo=timezone.utc) if m else None)
    target = made + timedelta(hours=args.lead)
    picked = pick_rows(rows, target)
    missing = [i for i in MAP_AIRPORTS if i not in picked]
    if missing:
        print(f"Hinweis: keine Daten fuer {', '.join(missing)}",
              file=sys.stderr)
    out = Path(args.out)
    draw_map(Path(args.map), out, picked, made, target, run_dt)

    # Archiv: Zeitstempel-Kopie, die ARCHIVE_KEEP juengsten behalten
    ARCHIVE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    arch = ARCHIVE_DIR / f"{out.stem}_{stamp}{out.suffix}"
    arch.write_bytes(out.read_bytes())
    old = sorted(ARCHIVE_DIR.glob(f"{out.stem}_*{out.suffix}"))[:-ARCHIVE_KEEP]
    for p in old:
        p.unlink()
        print(f"Archiv geloescht: {p.name}")

    got = picked[next(iter(picked))]["valid"]
    print(f"Karte geschrieben: {out} + Archiv {arch.name} "
          f"(Briefing {made:%d.%m. %H:%M}Z, Zeile ~{got:%d.%m. %H}Z)")


if __name__ == "__main__":
    main()
