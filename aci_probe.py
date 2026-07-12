#!/usr/bin/env python3
"""aci_probe.py — Einmal-Messung: Wie fein ist das ACI-Rohprodukt wirklich?

Das GIF (arcticcomposite-vis-xlarge.gif) ist ein Rendering von 2048x2048 px
ueber 50-90N, also ~3 km/px. Im selben AMRDC-Verzeichnis liegen die
Rohprodukte:

  ArcticCompositeVisible.nc.gz        ~32 MB  netCDF (gepackt)
  ArcticCompositeVisibleAwips2.nc     ~4.5 MB netCDF (AWIPS2)
  ArcticCompositeVisible.area         ~4.5 MB McIDAS AREA

Dieses Skript laedt sie, liest Gittergroesse, Variablen und
Projektionsattribute aus und schreibt alles ins Log. Danach ist
entschieden, ob sich ein Umstieg auf das Rohgitter lohnt — und der
AREA-Navigationsblock liefert die Projektionsparameter amtlich, statt
dass wir sie wie beim GIF gegen Kuestenlinien fitten muessen.

Kein Bestandteil des stuendlichen Betriebs: nur manuell (workflow_dispatch).

    python aci_probe.py
"""
from __future__ import annotations

import gzip
import io
import struct
import sys

import httpx

BASE = ("https://amrdc.ssec.wisc.edu/web_products/"
        "satellite_imagery/arctic/")
UA = {"User-Agent": "Mozilla/5.0 (TTA-Expedition aci-probe; "
      "+https://github.com/sscherergo/tta)"}

TARGETS = [
    "ArcticCompositeVisibleAwips2.nc",
    "ArcticCompositeVisible.area",
    "ArcticCompositeVisible.nc.gz",
]


def fetch(client: httpx.Client, name: str) -> bytes | None:
    try:
        r = client.get(BASE + name, headers=UA, follow_redirects=True)
    except httpx.HTTPError as e:
        print(f"[probe] {name}: {e}")
        return None
    if r.status_code != 200:
        print(f"[probe] {name}: HTTP {r.status_code}")
        return None
    print(f"[probe] {name}: {len(r.content)/1e6:.1f} MB geladen")
    return r.content


def probe_netcdf(name: str, data: bytes) -> None:
    """Gittergroesse + Projektionsattribute. netCDF3 und netCDF4/HDF5
    werden beide von netCDF4-python gelesen (in-memory)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
        print(f"  entpackt: {len(data)/1e6:.1f} MB")
    magic = data[:4]
    print(f"  Magic: {magic!r} "
          f"({'netCDF3' if magic[:3] == b'CDF' else 'HDF5/netCDF4'})")
    try:
        from netCDF4 import Dataset            # nur im Probe-Lauf noetig
    except ImportError:
        print("  netCDF4 nicht installiert — Dimensionen nicht lesbar")
        return
    ds = Dataset("inmem", mode="r", memory=data)
    print(f"  Dimensionen: "
          + ", ".join(f"{k}={len(v)}" for k, v in ds.dimensions.items()))
    for vn, v in ds.variables.items():
        print(f"  Variable {vn}: shape={v.shape} dtype={v.dtype}")
        for a in v.ncattrs():
            val = str(v.getncattr(a))[:120]
            print(f"      {a} = {val}")
    print("  Globale Attribute:")
    for a in ds.ncattrs():
        print(f"      {a} = {str(ds.getncattr(a))[:120]}")
    ds.close()


def probe_area(data: bytes) -> None:
    """McIDAS AREA: 64 Woerter Directory. Wort 8 = Zeilen, Wort 9 =
    Elemente, Wort 11/12 = Aufloesung (Sampling), Wort 35 = Nav-Typ.
    Byte-Reihenfolge aus Wort 1 (Sentinel = 4) ableiten."""
    head = data[:256]
    for endian, tag in (("<", "little"), (">", "big")):
        words = struct.unpack(endian + "64i", head)
        if words[1] == 4:                       # Wort 2 = Formatkennung
            print(f"  Byte-Reihenfolge: {tag}")
            print(f"  Zeilen x Elemente: {words[8]} x {words[9]}")
            print(f"  Bytes je Element: {words[10]}")
            print(f"  Zeilen-/Element-Sampling: {words[11]} / {words[12]}")
            print(f"  Bildzeit (yyddd/hhmmss): {words[3]} / {words[4]}")
            nav = data[words[34]:words[34] + 4] if words[34] else b""
            print(f"  Navigationstyp: {nav!r}  "
                  f"(PS = Polstereografie — enthaelt die amtlichen "
                  f"Projektionsparameter)")
            return
    print("  AREA-Header nicht erkannt (kein Sentinel 4 in Wort 2)")


def main() -> None:
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        for name in TARGETS:
            print(f"\n=== {name}")
            data = fetch(client, name)
            if data is None:
                continue
            if name.endswith(".area"):
                probe_area(data)
            else:
                probe_netcdf(name, data)
    print("\nVergleich: das GIF ist 2048x2048 ueber 50-90N (~3 km/px). "
          "Ist das Rohgitter groesser, liegt dort die Reserve.")


if __name__ == "__main__":
    main()
