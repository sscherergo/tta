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


def gfa(region: str, panel: str) -> dict:
    page = f"Latest-gfacn{region}_cldwx_{panel}-e"
    return {"name": f"gfacn{region}_{panel}.jpg",
            "direct": [base + page + ".jpg" for base in AWWS],
            "wrapper": [base + page + ".html" for base in AWWS]}


CHARTS: list[dict] = [
    *(gfa("37", p) for p in ("000", "006", "012")),
    *(gfa("36", p) for p in ("000", "006", "012")),
    *(gfa("35", p) for p in ("000", "006")),
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


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ok = changed = 0
    with httpx.Client(timeout=TIMEOUT, verify=True) as client:
        for c in CHARTS:
            data = None
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
                print(f"FEHLT: {c['name']} — alle Quellen verweigert")
                continue
            ok += 1
            path = OUT / c["name"]
            new = hashlib.sha256(data).hexdigest()
            old = (hashlib.sha256(path.read_bytes()).hexdigest()
                   if path.exists() else "")
            if new != old:
                path.write_bytes(data)
                changed += 1
    print(f"Spiegel: {ok}/{len(CHARTS)} Charts geholt, "
          f"{changed} aktualisiert.")
    if ok == 0:
        sys.exit(1)                    # Totalausfall soll rot werden


if __name__ == "__main__":
    main()
