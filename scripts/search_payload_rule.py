"""Search for a deterministic derivation of the per-device meter constants.

Targets (4 real meters, all values verified against live reads):

    SN -> (read payload byte s, response XOR)

The transport encoding is known; this script only searches for *where s and the
XOR come from*.  A candidate must match all four devices at once; with four
samples a false positive is still possible but unlikely for the searched spaces.

Run:  python scripts/search_payload_rule.py
"""

from __future__ import annotations

import hashlib
import zlib
from functools import reduce

import numpy as np

# SN, MAC, payload byte s, response XOR
DATA = [
    ("KK01264I12F4", "26:04:18:01:12:F4", 0xA4, 0x2C, 1984007, 1265025),
    ("KK04261F2F96", "26:01:15:04:2F:96", 0x5A, 0x0D, 2001508, 1273842),
    ("KK01264I0311", "26:04:18:01:03:11", 0xD0, 0x79, 2001516, 1273846),
    ("KK01264I0316", "26:04:18:01:03:16", 0xB0, 0x3F, 2001517, 1273847),
]

TARGET_S = [d[2] for d in DATA]
TARGET_X = [d[3] for d in DATA]


def mac_bytes(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def variants(sn: str, mac: str, id1: int, id2: int) -> dict[str, bytes]:
    m = mac_bytes(mac)
    return {
        "sn": sn.encode(),
        "sn2": sn[2:].encode(),
        "sn4": sn[-4:].encode(),
        "sn4hex": bytes.fromhex(sn[-4:]),
        "mac": m,
        "macrev": m[::-1],
        "machex": mac.replace(":", "").lower().encode(),
        "macsn": m + sn.encode(),
        "snmac": sn.encode() + m,
        "id_detail": str(id1).encode(),
        "id_unit": str(id2).encode(),
        "id_unit_hex": format(id2, "x").encode(),
    }


def crc8(data: bytes, poly: int, init: int, refin: bool) -> int:
    c = init
    for b in data:
        if refin:
            c ^= b
            for _ in range(8):
                c = (c >> 1) ^ poly if c & 1 else c >> 1
        else:
            c ^= b
            for _ in range(8):
                c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c & 0xFF


def fnv1a32(d: bytes) -> int:
    h = 0x811C9DC5
    for b in d:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def fnv1_32(d: bytes) -> int:
    h = 0x811C9DC5
    for b in d:
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    return h


def djb2(d: bytes) -> int:
    h = 5381
    for b in d:
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


def sdbm(d: bytes) -> int:
    h = 0
    for b in d:
        h = (b + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
    return h


def java_hash(d: bytes) -> int:
    h = 0
    for b in d:
        h = (h * 31 + b) & 0xFFFFFFFF
    return h


def search_crc8(vlist):
    hits = []
    for poly in range(1, 256):
        for init in (0, 0xFF):
            for refin in (False, True):
                for name, vals in vlist.items():
                    if not vals[0]:
                        continue
                    out = [crc8(v, poly, init, refin) for v in vals]
                    if out == TARGET_S:
                        hits.append(("s", "crc8", name, poly, init, refin))
                    if out == TARGET_X:
                        hits.append(("xor", "crc8", name, poly, init, refin))
    return hits


def search_hashes(vlist):
    hits = []
    funcs = {
        "crc32": zlib.crc32,
        "adler32": zlib.adler32,
        "fnv1a": fnv1a32,
        "fnv1": fnv1_32,
        "djb2": djb2,
        "sdbm": sdbm,
        "java": java_hash,
    }
    for name, vals in vlist.items():
        if not vals[0]:
            continue
        for fname, fn in funcs.items():
            cs = [fn(v) & 0xFFFFFFFF for v in vals]
            views = {
                "b0": [c & 0xFF for c in cs],
                "b1": [(c >> 8) & 0xFF for c in cs],
                "b2": [(c >> 16) & 0xFF for c in cs],
                "b3": [(c >> 24) & 0xFF for c in cs],
                "fold": [((c ^ (c >> 8) ^ (c >> 16) ^ (c >> 24)) & 0xFF) for c in cs],
            }
            for vname, out in views.items():
                if out == TARGET_S:
                    hits.append(("s", fname, name, vname))
                if out == TARGET_X:
                    hits.append(("xor", fname, name, vname))
        for algo in ("md5", "sha1", "sha256", "sha512", "blake2b", "blake2s"):
            digs = [hashlib.new(algo, v).digest() for v in vals]
            for pos in range(len(digs[0])):
                out = [d[pos] for d in digs]
                if out == TARGET_S:
                    hits.append(("s", algo, name, pos))
                if out == TARGET_X:
                    hits.append(("xor", algo, name, pos))
    return hits


def crc16_all_polys(data: bytes, init: int, refin: bool) -> np.ndarray:
    """CRC16 of one input for every 16-bit polynomial, vectorised."""
    polys = np.arange(65536, dtype=np.uint32)
    state = np.full(65536, init & 0xFFFF, dtype=np.uint32)
    for byte in data:
        state = (state ^ (byte if refin else (byte << 8))) & 0xFFFF
        for _ in range(8):
            if refin:
                state = np.where(state & 1, (state >> 1) ^ polys, state >> 1) & 0xFFFF
            else:
                state = np.where(
                    state & 0x8000,
                    ((state << 1) & 0xFFFF) ^ polys,
                    (state << 1) & 0xFFFF,
                ) & 0xFFFF
    return state


def search_crc16(vlist):
    hits = []
    for name, vals in vlist.items():
        if not vals[0]:
            continue
        for init in (0, 0xFFFF):
            for refin in (False, True):
                states = [crc16_all_polys(v, init, refin) for v in vals]
                views = {
                    "lo": [s & 0xFF for s in states],
                    "hi": [s >> 8 for s in states],
                    "fold": [((s ^ (s >> 8)) & 0xFF) for s in states],
                }
                for vname, arr in views.items():
                    for tgt, label in ((TARGET_S, "s"), (TARGET_X, "xor")):
                        mask = arr[0] == tgt[0]
                        for i in range(1, len(arr)):
                            mask &= arr[i] == tgt[i]
                        idx = np.nonzero(mask)[0]
                        if len(idx):
                            hits.append((label, "crc16", name, init, refin, vname,
                                         [int(x) for x in idx[:8]]))
    return hits


def search_sums(vlist):
    hits = []
    for name, vals in vlist.items():
        if not vals[0]:
            continue
        views = {
            "sum": [sum(v) & 0xFF for v in vals],
            "xor": [reduce(lambda a, b: a ^ b, v, 0) for v in vals],
        }
        for vname, out in views.items():
            if out == TARGET_S:
                hits.append(("s", "sum", name, vname))
            if out == TARGET_X:
                hits.append(("xor", "sum", name, vname))
    return hits


def main() -> None:
    vlist = {name: [variants(sn, mac, i1, i2)[name]
                    for sn, mac, _s, _x, i1, i2 in DATA]
             for name in variants(*DATA[0][:2], DATA[0][4], DATA[0][5])}
    hits = []
    hits += search_crc8(vlist)
    hits += search_hashes(vlist)
    hits += search_sums(vlist)
    hits += search_crc16(vlist)
    if hits:
        print("HITS:")
        for h in hits:
            print(" ", h)
    else:
        print("no match in the searched spaces (CRC8 all polys, CRC16 all polys, "
              "CRC32/Adler/FNV/djb2/sdbm/Java, MD5/SHA/BLAKE2 bytes, sums)")


if __name__ == "__main__":
    main()
