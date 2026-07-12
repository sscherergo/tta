#!/usr/bin/env python3
"""aci_mirror.py — Spiegelt das stuendliche NOAA Arctic Composite (ACI).

Quelle: OSPO/STAR/CIMSS, satepsanone.nesdis.noaa.gov/pub/7day/arctic/
(offenes Verzeichnis, GIF je Kanal/Stunde). Kanaele: Visible + IR ~11um.
Latenz laut Produktseite 2-3 h; Zeitstempel steht auf dem Bild.

Ausgabe: aci_vis.gif / aci_ir.gif im Arbeitsverzeichnis — der Workflow
pusht sie auf den Orphan-Branch `aci` (Historie waechst nicht).
Dateinamen im Verzeichnis werden zur Laufzeit entdeckt (Discovery),
bei Muster-Fehlschlag landen die echten Namen im Log.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from urllib.parse import urljoin

import httpx

BASES = ["https://satepsanone.nesdis.noaa.gov/pub/7day/arctic/",
         "http://satepsanone.nesdis.noaa.gov/pub/7day/arctic/"]
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition aci-mirror; "
      "+https://github.com/sscherergo/tta)"}
TIMEOUT = httpx.Timeout(60.0)
MIN_BYTES = 30_000

# Kanalwahl: Token, die im Dateinamen vorkommen muessen/duerfen.
CHANNELS = {
    "aci_vis.gif": {"must": ("vis",), "not": ()},
    "aci_ir.gif":  {"must": ("ir",),
                    "not": ("sir", "swir", "wv", "vis", "lwir", "ir12")},
}


def newest(names: list[str], spec: dict) -> str | None:
    ok = [n for n in names
          if all(t in n.lower() for t in spec["must"])
          and not any(t in n.lower() for t in spec["not"])]
    return sorted(ok)[-1] if ok else None


def main() -> None:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        hrefs: list[str] = []
        base_used = ""
        for base in BASES:
            try:
                r = client.get(base, headers=UA)
            except httpx.HTTPError as e:
                print(f"[aci] {base}: {e}")
                continue
            if r.status_code != 200:
                print(f"[aci] {base}: HTTP {r.status_code}")
                continue
            # tolerant: absolute/relative Pfade, GIF/JPG/PNG, jede Schreibung
            hrefs = sorted({h for h in
                            re.findall(r'href="([^"]+\.(?:gif|jpe?g|png))"',
                                       r.text, re.I)})
            base_used = base
            if hrefs:
                break
            all_links = re.findall(r'href="([^"]+)"', r.text)[:10]
            print(f"[aci] {base}: 200, aber keine Bild-Links — "
                  f"{len(r.text)} B, erste Links: {all_links}")
            if not all_links:
                print(f"[aci] Body-Anfang: "
                      + re.sub(r"\s+", " ", r.text)[:250])
        if not hrefs:
            print("[aci] kein Verzeichnis lesbar — Abbruch (Diagnose oben)")
            sys.exit(1)
        names = [h.split("/")[-1] for h in hrefs]
        by_name = dict(zip(names, hrefs))

        got = 0
        for out_name, spec in CHANNELS.items():
            pick = newest(names, spec)
            if pick is None:
                print(f"[aci] kein Treffer fuer {out_name} — "
                      f"Beispiele im Index: {names[-8:]}")
                continue
            try:
                r = client.get(urljoin(base_used, by_name[pick]),
                               headers=UA)
            except httpx.HTTPError as e:
                print(f"[aci] {pick}: {e}")
                continue
            if r.status_code != 200 or len(r.content) < MIN_BYTES:
                print(f"[aci] {pick}: HTTP {r.status_code}, "
                      f"{len(r.content)} B")
                continue
            path = Path(out_name)
            new = hashlib.sha256(r.content).hexdigest()
            old = (hashlib.sha256(path.read_bytes()).hexdigest()
                   if path.exists() else "")
            if new != old:
                path.write_bytes(r.content)
                print(f"[aci] {pick} -> {out_name} "
                      f"({len(r.content)//1024} KB)")
            else:
                print(f"[aci] {pick}: unveraendert")
            got += 1
        print(f"ACI: {got}/{len(CHANNELS)} Kanaele geholt.")
        if got == 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
