#!/usr/bin/env python3
"""sigwx_mirror.py — Spiegelt amtliche SIGWX/GFA-Charts ins Repo.

Quellen: NAV CANADA GFA (CFPS-Bild-API), NWS/AAWU Alaska,
IMO/vedur.is (WAFC-NAT + Island).
Grund fuer den Spiegel: Hotlink-/Referer-Schutz und Mixed-Content
blockieren Direkteinbettung im Browser; der Runner holt serverseitig.

Ausgabe: sigwx/<name> — geschrieben nur bei Aenderung (Hash-Vergleich).
Teilausfaelle sind non-fatal: jede Quelle wird unabhaengig versucht.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
import httpx

OUT = Path("sigwx")
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition wx-mirror; "
      "+https://github.com/sscherergo/tta)"}
TIMEOUT = httpx.Timeout(30.0)
MIN_BYTES = 15_000                    # kleiner = Fehlerseite, nicht Chart

# CFPS (plan.navcanada.ca): aktuelles NAV-CANADA-System. Die Weather-API
# liefert je Punkt die GFA-Daten der abdeckenden Region als JSON; die
# Bilder liegen unter /weather/images/{id}.image.
CFPS_API = "https://plan.navcanada.ca/weather/api/alpha/"
CFPS_IMG = "https://plan.navcanada.ca/weather/images/{id}.image"
REGION_POINT = {                       # repraesentativer Platz je Region
    "37": ("CYRB", "-94.969,74.717"),
    "36": ("CYFB", "-68.556,63.756"),
    "35": ("CYEV", "-133.483,68.304"),
}
PANEL_INDEX = {"000": 0, "006": 1, "012": 2}


def gfa_via_cfps(client: httpx.Client, region: str,
                 panels: tuple[str, ...]) -> dict[str, bytes]:
    """Gewuenschte Panels einer GFA-Region (Clouds & Weather) ueber die
    CFPS-API. Rueckgabe: {'000': bytes, ...} — leer bei Fehlschlag
    (Diagnose im Log)."""
    icao, lonlat = REGION_POINT[region]
    out: dict[str, bytes] = {}
    # Die CFPS-API ist inoffiziell — Parameterform variiert. Alle
    # bekannten Varianten durchprobieren, bis eine Zeilen liefert:
    variants = [
        {"site": icao, "image": "GFA/CLDWX"},
        {"point": lonlat, "image": "GFA/CLDWX"},
        {"point": f"{icao}|site|{lonlat}", "image": "GFA/CLDWX"},
        {"site": icao, "alpha": "gfa"},
        {"point": lonlat, "alpha": "gfa"},
    ]
    rows: list = []
    for params in variants:
        try:
            r = client.get(CFPS_API,
                           headers={**UA, "Accept": "application/json",
                                    "Referer": "https://plan.navcanada.ca/gfa/"},
                           params=params, follow_redirects=True)
            if r.status_code != 200:
                print(f"  [cfps] GFACN{region} {params}: HTTP {r.status_code}")
                continue
            rows = r.json().get("data", [])
            if rows:
                print(f"  [cfps] GFACN{region}: Variante {list(params)[0]}="
                      f"{list(params.values())[0]} -> {len(rows)} Zeilen")
                break
            print(f"  [cfps] GFACN{region} {params}: 0 Zeilen")
        except (httpx.HTTPError, ValueError) as e:
            print(f"  [cfps] GFACN{region} {params}: {e}")
    if not rows:
        return out

    row = None
    for cand in rows:
        blob = json.dumps(cand).lower()
        if f"gfacn{region}".lower() in blob and "cldwx" in blob:
            row = cand
            break
    if row is None:                     # Notnagel: erste Region-Zeile
        row = next((c for c in rows
                    if f"gfacn{region}".lower() in json.dumps(c).lower()),
                   None)
    if row is None:
        print(f"  [cfps] GFACN{region}: keine passende Zeile — "
              f"Antwort-Zeilen: "
              + str([c.get('location') for c in rows])[:200])
        return out

    try:
        payload = row.get("text")
        payload = json.loads(payload) if isinstance(payload, str) else payload
        fls = payload["frame_lists"]
        fl = max(fls, key=lambda f: str(f.get("sv") or f.get("id")))
        frames = fl["frames"]
    except (KeyError, TypeError, ValueError) as e:
        print(f"  [cfps] GFACN{region}: Strukturfehler {e} — "
              f"Schluessel: {list(row)[:10]}, text[:200]="
              + str(row.get('text'))[:200])
        return out

    for panel in panels:
        idx = PANEL_INDEX[panel]
        if idx >= len(frames):
            continue
        try:
            img_id = max(im["id"] for im in frames[idx]["images"])
        except (KeyError, ValueError, TypeError):
            print(f"  [cfps] GFACN{region}/{panel}: keine Bild-ID")
            continue
        data = get(client, CFPS_IMG.format(id=img_id))
        if data:
            out[panel] = data
            print(f"  via CFPS: GFACN{region} {panel} (id {img_id})")
    return out


CHARTS: list[dict] = [
    *({"name": f"aawu_sigwx{h}.png",
       "direct": [f"https://www.weather.gov/images/aawu/sigWx{h}.png"],
       "wrapper": []} for h in (24, 36, 48)),
    # Mid-Level SIGWX Nordatlantik (FL100-450, WAFC Washington):
    # Dateinamen werden zur Laufzeit aus dem offenen AWC-Verzeichnis
    # ermittelt (awc_swm_nat), hier nur Platzhalter fuer die Zaehlung.
    *({"name": f"iceland_{v}.png",
       "direct": [f"https://www.vedur.is/photos/flugkort/sigwx_iceland_{v}.png",
                  f"http://www.vedur.is/photos/flugkort/sigwx_iceland_{v}.png"],
       "wrapper": []} for v in ("06", "12", "18")),
]

AWC_SWM_DIRS = ["https://aviationweather.gov/data/products/swm/",
                "https://www.aviationweather.gov/data/products/swm/"]
SWM_SLOTS = 4



SIGWX_KEYS = [                      # Produktschluessel-Kandidaten (CFPS)
    "SIGWX/MID_LEVEL_ATLANTIC", "SIGWX/MID_LEVEL_CANADA",
    "SIGWX/MID_ATLANTIC", "SIGWX/MID_CANADA",
    "SIG_WX/MID_LEVEL_ATLANTIC", "SIG_WX/MID_LEVEL_CANADA",
    "SIGWX/MIDLVL_ATLANTIC", "SIGWX/MIDLVL_CANADA", "SIGWX",
]


def cfps_sigwx(client: httpx.Client) -> dict[str, bytes]:
    """Mid-Level-SIGWX (Atlantik + Kanada) ueber die CFPS-API — gleiche
    Mechanik wie die GFA. Produktschluessel unbekannt -> Kandidaten
    durchprobieren; jede 200er-Antwort mit Zeilen loggt die echten
    image/location-Werte (Selbstdiagnose)."""
    out: dict[str, bytes] = {}
    rows_all: list = []
    for key in SIGWX_KEYS:
        try:
            r = client.get(CFPS_API,
                           headers={**UA, "Accept": "application/json"},
                           params={"site": "CYFB", "image": key},
                           follow_redirects=True)
            if r.status_code != 200:
                continue
            rows = r.json().get("data", [])
            if rows:
                print(f"  [cfps-sigwx] image={key} -> {len(rows)} Zeilen; "
                      f"Produkte: "
                      + str([(c.get('image'), c.get('location'))
                             for c in rows])[:220])
                rows_all += rows
                if len({json.dumps(c)[:80] for c in rows_all}) >= 2                         and key != "SIGWX":
                    break
        except (httpx.HTTPError, ValueError) as e:
            print(f"  [cfps-sigwx] image={key}: {e}")
    if not rows_all:
        print("  [cfps-sigwx] kein Schluessel lieferte Zeilen")
        return out

    def frames_of(row):
        payload = row.get("text")
        payload = json.loads(payload) if isinstance(payload, str) else payload
        fls = payload["frame_lists"]
        fl = max(fls, key=lambda f: str(f.get("sv") or f.get("id")))
        return fl["frames"]

    k = 0
    for want in ("atlantic", "canada"):
        row = next((c for c in rows_all
                    if want in json.dumps(c).lower()), None)
        if row is None:
            print(f"  [cfps-sigwx] kein {want}-Produkt in den Zeilen")
            continue
        try:
            frames = frames_of(row)
        except (KeyError, TypeError, ValueError) as e:
            print(f"  [cfps-sigwx] {want}: Strukturfehler {e}")
            continue
        for fr in frames[:2]:
            try:
                img_id = max(im["id"] for im in fr["images"])
            except (KeyError, ValueError, TypeError):
                continue
            data = get(client, CFPS_IMG.format(id=img_id))
            if data:
                k += 1
                out[f"swm_nat_{k}.png"] = data
                print(f"  via CFPS-SIGWX: {want} (id {img_id}) "
                      f"-> swm_nat_{k}.png")
    return out

def awc_swm_nat(client: httpx.Client) -> dict[str, bytes]:
    """Mid-Level-SIGWX NAT (FL100-450) aus dem offenen AWC-Verzeichnis.
    Ermittelt die aktuellen Dateinamen selbst (Muster *nat* ohne
    Datums-Praefix) und liefert bis zu SWM_SLOTS Charts als
    {'swm_nat_1.png': bytes, ...}."""
    out: dict[str, bytes] = {}

    # Stufe -1: CFPS-SIGWX (gleiche API wie GFA — Stefan-Fund 12.07.)
    out = cfps_sigwx(client)
    if out:
        return out

    # Stufe 0: NOAA-tgftp (offener Faxserver, keine WAF/Geo-Sperre).
    # 14er-Serie = WAFC-Mid-Level-PNGs; Region steht in der Kartenlegende
    # (PGNE14 = NAT-Kandidat Nr. 1). PGAE05 = High-Level, bewusst NICHT.
    TGFTP = "https://tgftp.nws.noaa.gov/fax/"
    for k, fname in enumerate(("PGNE14.PNG", "PGCE14.PNG",
                               "PGDE14.PNG", "PGZE14.PNG"), 1):
        data = get(client, TGFTP + fname)
        if data:
            out[f"swm_nat_{k}.png"] = data
            print(f"  via tgftp: {fname} -> swm_nat_{k}.png")
    if out:
        return out

    names: list[str] = []
    base_used = ""
    for base in AWC_SWM_DIRS:                 # www-Redirect-Schleifen umgehen
        try:
            r = client.get(base, headers=UA, follow_redirects=True)
            if r.status_code != 200:
                print(f"  [awc] {base}: HTTP {r.status_code}")
                continue
            names = sorted({
                n for n in re.findall(r'href="([^"/?]+\.(?:png|gif))"', r.text)
                if "nat" in n.lower() and not re.match(r"^\d{8}", n)})
            base_used = base
            if names:
                break
            print(f"  [awc] {base}: Index ohne *nat*-Dateien — "
                  f"erste Links: "
                  f"{re.findall(chr(39)+'href="([^"]+)"'+chr(39), r.text)[:6]}")
        except httpx.HTTPError as e:
            print(f"  [awc] {base}: {e}")

    if not names:
        # Stufe 2: Bild-URLs aus der Prog-Chart-Seite extrahieren —
        # der US-Runner passiert AWCs Geo-/WAF-Filter, EU-Browser nicht.
        pages = ["https://aviationweather.gov/progchart/mid",
                 "https://www.aviationweather.gov/progchart/mid"]
        for page in pages:
            try:
                r = client.get(page, headers=UA, follow_redirects=True)
            except httpx.HTTPError as e:
                print(f"  [awc] {page}: {e}")
                continue
            if r.status_code != 200:
                print(f"  [awc] {page}: HTTP {r.status_code}")
                continue
            cands = re.findall(
                r'(?:src|href)="([^"]+\.(?:png|gif|jpg))"', r.text)
            hits = [c for c in cands
                    if any(t in c.lower() for t in ("swm", "nat", "mid"))]
            if not hits:
                print(f"  [awc] {page}: keine passenden Bilder — "
                      f"Kandidaten: {cands[:8]}")
                continue
            for k, src in enumerate(dict.fromkeys(hits)[:SWM_SLOTS]
                                    if isinstance(hits, dict) else
                                    list(dict.fromkeys(hits))[:SWM_SLOTS], 1):
                url = src if src.startswith("http")                     else "https://aviationweather.gov" + src
                data = get(client, url)
                if data:
                    out[f"swm_nat_{k}.png"] = data
                    print(f"  via Seite: {url} -> swm_nat_{k}.png")
            return out
    if not names:
        print("  [awc] keine *nat*-Dateien im Index — Struktur geaendert?")
        return out
    for k, n in enumerate(names[:SWM_SLOTS], 1):
        data = get(client, base_used + n)
        if data:
            out[f"swm_nat_{k}.png"] = data
            print(f"  via AWC: {n} -> swm_nat_{k}.png")
    return out

def get(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url, headers=UA, follow_redirects=True)
        if r.status_code == 200 and len(r.content) >= MIN_BYTES \
                and not r.headers.get("content-type", "").startswith("text/html"):
            return r.content
    except httpx.HTTPError as e:
        print(f"  [http] {url}: {e}")
    return None


def save(data: bytes, name: str) -> bool:
    path = OUT / name
    new = hashlib.sha256(data).hexdigest()
    old = (hashlib.sha256(path.read_bytes()).hexdigest()
           if path.exists() else "")
    if new != old:
        path.write_bytes(data)
        return True
    return False


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ok = changed = total = 0
    with httpx.Client(timeout=TIMEOUT, verify=True) as client:
        # --- GFA: CFPS zuerst, Legacy-AWWS als Rueckfall je Panel ---
        gfa_panels = {"37": ("000", "006", "012"),
                      "36": ("000", "006", "012"),
                      "35": ("000", "006")}
        for region, panels in gfa_panels.items():
            total += len(panels)
            got = gfa_via_cfps(client, region, panels)
            for panel in panels:
                data = got.get(panel)
                if data is None:
                    print(f"FEHLT: gfacn{region}_{panel}.jpg — "
                          f"CFPS ohne Treffer (Diagnose oben)")
                    continue
                ok += 1
                if save(data, f"gfacn{region}_{panel}.jpg"):
                    changed += 1

        # --- Mid-Level SIGWX NAT (FL100-450, WAFC Washington) ---
        total += SWM_SLOTS
        swm = awc_swm_nat(client)
        for k in range(1, SWM_SLOTS + 1):
            name = f"swm_nat_{k}.png"
            data = swm.get(name)
            if data is None:
                print(f"FEHLT: {name} — AWC-Verzeichnis (Diagnose oben)")
                continue
            ok += 1
            if save(data, name):
                changed += 1

        # --- Uebrige Charts (AAWU, Island): Direktabruf ---
        for c in CHARTS:
            total += 1
            data = None
            for u in c["direct"]:
                data = get(client, u)
                if data:
                    break
            if data is None:
                print(f"FEHLT: {c['name']} — alle Quellen verweigert")
                continue
            ok += 1
            if save(data, c["name"]):
                changed += 1
    print(f"Spiegel: {ok}/{total} Charts geholt, {changed} aktualisiert.")
    if ok == 0:
        sys.exit(1)                    # Totalausfall soll rot werden


if __name__ == "__main__":
    main()
