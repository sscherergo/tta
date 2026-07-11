#!/usr/bin/env python3
"""sigwx_mirror.py — Spiegelt amtliche SIGWX/GFA-Charts ins Repo.

Quellen: NAV CANADA GFA (ueber Wrapper-Seite, Bild-URL dynamisch
extrahiert), NWS/AAWU Alaska, IMO/vedur.is (WAFC-NAT + Island).
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
from urllib.parse import urljoin

import httpx

OUT = Path("sigwx")
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition wx-mirror; "
      "+https://github.com/sscherergo/tta)"}
TIMEOUT = httpx.Timeout(30.0)
MIN_BYTES = 15_000                    # kleiner = Fehlerseite, nicht Chart

AWWS = ["https://plandevol.navcanada.ca/Latest/gfa/anglais/",
        "https://flightplanning.navcanada.ca/Latest/gfa/anglais/"]

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
        {"point": f"{icao}|site|{lonlat}", "alpha": "gfa"},
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


def gfa(region: str, panel: str) -> dict:
    page = f"Latest-gfacn{region}_cldwx_{panel}-e"
    return {"name": f"gfacn{region}_{panel}.jpg",
            "direct": [base + page + ".jpg" for base in AWWS],
            "wrapper": [base + page + ".html" for base in AWWS]}


CHARTS: list[dict] = [
    *({"name": f"aawu_sigwx{h}.png",
       "direct": [f"https://www.weather.gov/images/aawu/sigWx{h}.png"],
       "wrapper": []} for h in (24, 36, 48)),
    *({"name": f"nat_{v}.png",
       "direct": [f"https://www.vedur.is/photos/flugkort/PGAE05_EGRR_{v}.png",
                  f"http://www.vedur.is/photos/flugkort/PGAE05_EGRR_{v}.png"],
       "wrapper": []} for v in ("0000", "0600", "1200", "1800")),
    *({"name": f"iceland_{v}.png",
       "direct": [f"https://www.vedur.is/photos/flugkort/sigwx_iceland_{v}.png",
                  f"http://www.vedur.is/photos/flugkort/sigwx_iceland_{v}.png"],
       "wrapper": []} for v in ("06", "12", "18")),
]

IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def get(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url, headers=UA, follow_redirects=True)
        if r.status_code == 200 and len(r.content) >= MIN_BYTES \
                and not r.headers.get("content-type", "").startswith("text/html"):
            return r.content
    except httpx.HTTPError as e:
        print(f"  [http] {url}: {e}")
    return None


def via_wrapper(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url, headers=UA, follow_redirects=True)
        if r.status_code != 200:
            return None
    except httpx.HTTPError as e:
        print(f"  [http] {url}: {e}")
        return None
    for src in IMG_RE.findall(r.text):
        if "gfa" in src.lower():
            img = get(client, urljoin(url, src))
            if img:
                print(f"  via Wrapper: {urljoin(url, src)}")
                return img
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
                    c = gfa(region, panel)
                    for u in c["direct"]:
                        data = get(client, u)
                        if data:
                            break
                    if data is None:
                        for w in c["wrapper"]:
                            data = via_wrapper(client, w)
                            if data:
                                break
                if data is None:
                    print(f"FEHLT: gfacn{region}_{panel}.jpg — "
                          f"CFPS und Legacy verweigert")
                    continue
                ok += 1
                if save(data, f"gfacn{region}_{panel}.jpg"):
                    changed += 1

        # --- Uebrige Charts (AAWU, NAT, Island): Direktabruf ---
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
