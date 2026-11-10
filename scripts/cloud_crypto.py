"""Crypto helpers for the iyunmu cloud BLE command API.

Recovered from the vendor WeChat mini-program (plugin ``ymdev``) and verified
against captured ``POST /api/v1/bleCommandV1`` traffic (2026-11).

Wire format for both request and response bodies::

    {"ciphertext": base64(hex(interleave(iv, aes_cbc(pkcs7(plain)))))} []

Derivation of the AES key (``key`` = x-api-key, ``secret`` = x-api-secret,
``sn`` = device serial, ``ts`` = unix seconds as sent in x-api-timestamp)::

    key_hex = SHA256((secret + hex(ts) + key + sn[2:] + hex(ts)).upper()).upper()

The interleave is: the 16-byte IV first, alternating with the first 16 bytes
of ciphertext (``iv[0], c[0], iv[1], c[1], ...``), then the remaining ciphertext
bytes verbatim.

The response upload (``isRes=1``) carries a signature computed by the client;
see :func:`response_sign`.  Request signatures (``isRes=0``) are issued by the
vendor REST API and cannot be generated locally.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def derive_key(secret: str, api_key: str, sn: str, ts: int) -> str:
    """Return the uppercase hex AES key for one request."""
    ts_hex = format(ts, "x")
    material = (secret + ts_hex + api_key + sn[2:] + ts_hex).upper()
    return hashlib.sha256(material.encode()).hexdigest().upper()


def encrypt_payload(secret: str, api_key: str, sn: str, ts: int, obj: Any) -> str:
    """Encrypt a JSON object into the ``ciphertext`` field value."""
    key = bytes.fromhex(derive_key(secret, api_key, sn, ts))
    iv = "".join(random.choice(string.ascii_uppercase) for _ in range(16)).encode()
    data = json.dumps(obj, separators=(",", ":")).encode()
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    interleaved = bytearray()
    for i in range(16):
        interleaved.append(iv[i])
        interleaved.append(ct[i])
    interleaved += ct[16:]
    return base64.b64encode(interleaved.hex().encode()).decode()


def decrypt_payload(secret: str, api_key: str, sn: str, ts: int, ciphertext: str) -> Any:
    """Decrypt a ``ciphertext`` field (request or response) back to JSON."""
    key = bytes.fromhex(derive_key(secret, api_key, sn, ts))
    raw = bytes.fromhex(base64.b64decode(ciphertext).decode("ascii"))
    iv = raw[0:32:2]
    ct = bytes(raw[1:32:2]) + raw[32:]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(ct) + dec.finalize()
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    return json.loads(plain)


def response_sign(access_key: str, pkt: bytes | list[int], ts: int, sn: str) -> str:
    """Signature for an ``isRes=1`` response upload.

    ``pkt`` is the raw BLE payload after frame decoding, byte order as read
    from the notification.  Result is Base64(uppercase SHA-256 hex).
    """
    body = "".join(format(b, "02x") for b in reversed(list(pkt)))
    material = body + str(ts) + access_key + sn[2:] + format(ts, "x")
    digest = hashlib.sha256(material.upper().encode()).hexdigest().upper()
    return base64.b64encode(digest.encode()).decode()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sn", required=True)
    ap.add_argument("--ts", type=int, required=True)
    ap.add_argument("--key", default=os.environ.get("YUNMU_API_KEY"))
    ap.add_argument("--secret", default=os.environ.get("YUNMU_API_SECRET"))
    ap.add_argument("--decrypt", metavar="CIPHERTEXT")
    ap.add_argument("--encrypt", metavar="JSON")
    args = ap.parse_args()
    if not args.key or not args.secret:
        ap.error("set YUNMU_API_KEY / YUNMU_API_SECRET or pass --key/--secret")
    if args.decrypt:
        print(json.dumps(decrypt_payload(args.secret, args.key, args.sn, args.ts, args.decrypt),
                         ensure_ascii=False, indent=2))
    elif args.encrypt:
        print(encrypt_payload(args.secret, args.key, args.sn, args.ts,
                              json.loads(args.encrypt)))
    else:
        ap.error("one of --encrypt/--decrypt is required")
