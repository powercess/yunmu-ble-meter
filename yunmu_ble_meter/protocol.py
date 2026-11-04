"""YunMu (云牧) BLE metering socket protocol — YunMu YM-S1-BES.

Reverse engineered from the 控控乐 mini-program (wx69980405ad3c47f6) and verified
against a physical device. See docs/protocol.md for the full write-up.

Frame layout on the wire (all bytes XORed with a MAC-derived key):

    [0][1]   magic      EC ED = V1 plain, CE DE = V2(AES)/V3, 93 92 = V0
    [2][3]   payload length, big endian
    [4..]    payload
    [n][n+1] CRC16 over payload, big endian

The XOR key and the CRC16 parameters are both derived from the device MAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "MAGIC_V0",
    "MAGIC_V1",
    "MAGIC_V2",
    "MAGIC_OPEN",
    "Frame",
    "Response",
    "ReplyType",
    "DeviceProfile",
    "KNOWN_PROFILES",
    "profile_for",
    "guess_payload_xor",
    "WH_PER_COUNT",
    "crc16",
    "crc_params",
    "build_frame",
    "parse_frame",
    "parse_response",
    "build_read",
    "build_set_threshold",
    "build_clear",
    "mac_from_advertised_name",
    "advertised_name",
    "frame_bytes_from_response",
    "DecodeError",
]

MAGIC_V0 = (0x93, 0x92)
MAGIC_V1 = (0xEC, 0xED)
MAGIC_V2 = (0xCE, 0xDE)
MAGIC_OPEN = (0xF0, 0x0F)

#: Energy represented by one increment of the device accumulator.
#:
#: Two independent observations agree within 5%:
#:   * device 2: app showed 0.16 kWh at counter=607  -> 0.2636 Wh/count
#:   * device 1: 16.7 W for 594 s (+2.76 Wh) moved the counter by 10 -> 0.276
#: The counter is a 16-bit register, so it wraps at 65536 ~ 17 kWh.
WH_PER_COUNT = 0.26

#: payload obfuscation applied on top of the per-frame XOR. This is NOT a
#: global constant: every meter uses its own value, and a wrong one makes the
#: payload decode to garbage (the frame CRC still validates, so this is easy to
#: miss). 0x2C is the value observed on the first device; see DeviceProfile.
PAYLOAD_XOR = 0x2C

#: threshold encoding mask (applied in the frame-domain payload)
THRESHOLD_MASK = 0xACAC

#: request opcodes, in frame-domain (i.e. after the MAC XOR has been removed)
CMD_READ = bytes.fromhex("a4aca4")
CMD_CLEAR = bytes.fromhex("abaead52a4")
CMD_CONFIG_HEAD = bytes.fromhex("83aa")
CMD_CONFIG_MID = bytes.fromhex("ac9eacac")


@dataclass(frozen=True)
class DeviceProfile:
    """Per-device protocol parameters.

    Most of the wire format is derived from the MAC, but two values are issued
    per device by the vendor cloud and cannot be computed locally:

    ``read_payload``
        The 3-byte payload of the read request, e.g. ``a4 ac a4``.

    ``payload_xor``
        The constant that de-obfuscates the response payload. A wrong value
        still passes the frame CRC — it only corrupts the logical fields — so
        this has to be captured, not guessed.

    Capture both by impersonating the device with a BLE peripheral and letting
    the vendor app issue one read (see ``tools/fake_meter``).
    """

    mac: str
    read_payload: bytes = CMD_READ
    payload_xor: int = PAYLOAD_XOR

    # Confidence markers: which values were actually observed on this device.
    observed_read_payload: bool = False
    observed_payload_xor: bool = False


#: Values captured from physical devices. Add entries as meters are probed.
KNOWN_PROFILES: dict[str, DeviceProfile] = {
    # 1号机, SN KK01264l12F4 — original reverse-engineering target
    "26:04:18:01:12:F4": DeviceProfile(
        mac="26:04:18:01:12:F4",
        read_payload=bytes.fromhex("a4aca4"),
        payload_xor=0x2C,
        observed_read_payload=True,
        observed_payload_xor=True,
    ),
    # 2号机, SN KK04261F2F96 — captured via BLE impersonation
    "26:01:15:04:2F:96": DeviceProfile(
        mac="26:01:15:04:2F:96",
        read_payload=bytes.fromhex("5a525a"),
        payload_xor=0x0D,
        observed_read_payload=True,
        observed_payload_xor=True,
    ),
    # 3号机, SN KK01264I0311 — both values captured and verified
    "26:04:18:01:03:11": DeviceProfile(
        mac="26:04:18:01:03:11",
        read_payload=bytes.fromhex("d0d8d0"),
        payload_xor=0x79,
        observed_read_payload=True,
        observed_payload_xor=True,
    ),
    # 4号机 — payload_xor recovered by structure brute force
    "26:04:18:01:03:16": DeviceProfile(
        mac="26:04:18:01:03:16",
        read_payload=bytes.fromhex("b0b8b0"),
        payload_xor=0x3F,
        observed_read_payload=True,
        observed_payload_xor=True,
    ),
}


def guess_payload_xor(payload: bytes, types: tuple[int, ...] = (0x08, 0x07)) -> int | None:
    """Recover the payload de-obfuscation constant from one captured reply payload.

    ``payload`` is the frame-domain payload (after the MAC XOR and CRC check).
    The constant is *not* recoverable from the CRC — that covers the obfuscated
    bytes — so the structure is used instead: try every constant and keep the one
    that yields a known reply type whose inner checksum is self-consistent.

    Returns ``None`` when no constant satisfies the structure (the payload is not
    a reply, or was captured mid-frame).
    """
    for candidate in range(256):
        plain = bytes(b ^ candidate for b in payload)
        if len(plain) < 3:
            continue
        reply_type, length = plain[0], plain[1]
        if reply_type not in types:
            continue
        if 2 + length + 1 > len(plain):
            continue
        if plain[2 + length] != (sum(plain[: 2 + length]) & 0xFF):
            continue
        return candidate
    return None


def profile_for(mac: str) -> DeviceProfile:
    """Return the captured profile for ``mac``, or a MAC-derived default."""
    key = mac.upper()
    prof = KNOWN_PROFILES.get(key)
    if prof is not None:
        return prof
    return DeviceProfile(mac=key)

#: the tail byte of a config frame is not derivable from the value; it varies per
#: command. Known samples: 1000->FE, 2000->92, 2100->0F, 3000->86, 9999->31
CONFIG_TAIL_SAMPLES = {1000: 0xFE, 2000: 0x92, 2100: 0x0F, 3000: 0x86, 9999: 0x31}


class DecodeError(ValueError):
    """Raised when a frame cannot be parsed or fails validation."""


class ReplyType(IntEnum):
    """Response opcodes observed on the device."""

    READ = 0x08
    CLEAR_ACK = 0x07


@dataclass(frozen=True)
class Frame:
    """A decoded request/response frame, in frame-domain."""

    magic: tuple[int, int]
    payload: bytes

    @property
    def kind(self) -> str:
        if self.magic == MAGIC_V1:
            return "v1"
        if self.magic == MAGIC_V2:
            return "v2"
        if self.magic == MAGIC_V0:
            return "v0"
        if self.magic == MAGIC_OPEN:
            return "open"
        return "unknown"


@dataclass(frozen=True)
class Response:
    """A parsed device response.

    Layout of the plaintext (payload XOR 0x2C)::

        [0]        reply type (0x08 = reading, 0x07 = clear ack)
        [1]        data length
        [2:2+n]    data
        [2+n]      checksum = sum(plain[0:2+n]) & 0xFF

    Note the counter offset differs between reply types — this is a quirk of the
    firmware, not a parsing bug. Verified against real captures:

        READ  (0x08): counter at plain[9:11]
        CLEAR (0x07): counter at plain[8:10]
    """

    type: int
    length: int
    data: bytes
    checksum: int
    plaintext: bytes

    def _u16(self, offset: int) -> int | None:
        """Big-endian 16-bit field at ``offset`` inside ``data``.

        Every measurement is 16 bits wide. Reading only the low byte silently
        truncates anything above 255 (a 297 W load reads as 15 W), which is why
        these used to be ``data[i] / scale``.
        """
        if self.type != ReplyType.READ or self.length < offset + 2:
            return None
        return (self.data[offset] << 8) | self.data[offset + 1]

    @property
    def counter(self) -> int:
        """16-bit device accumulator (energy, wraps at 65536, zeroed by clear).

        Confirmed to be the energy counter: it only advances under load, stops
        when the load is removed, and the app's "clear total" writes it to 0.
        """
        if self.type == ReplyType.READ:
            return (self.data[7] << 8) | self.data[8]
        return (self.data[6] << 8) | self.data[7]

    @property
    def device_energy_kwh(self) -> float:
        """Cumulative energy reported by the device itself, in kWh.

        This is the meter's own accumulator scaled by :data:`WH_PER_COUNT`, so
        unlike a locally integrated total it survives an exporter restart and
        is zeroed only when the app clears the meter.
        """
        return self.counter * WH_PER_COUNT / 1000.0

    @property
    def voltage(self) -> float | None:
        """Volts, or ``None`` if this reply carries no measurement."""
        v = self._u16(11)
        return None if v is None else v / 10.0

    @property
    def power(self) -> float | None:
        """Active power in watts."""
        v = self._u16(9)
        return None if v is None else v / 10.0

    @property
    def current(self) -> float | None:
        """Current in amperes."""
        v = self._u16(13)
        return None if v is None else v / 1000.0

    @property
    def power_factor(self) -> float | None:
        """Power factor (0..1)."""
        if self.type != ReplyType.READ or self.length < 16:
            return None
        return self.data[15] / 100.0

    @property
    def threshold(self) -> int | None:
        """Alarm threshold in watts, as echoed back by the device."""
        return self._u16(16)


def mac_bytes(mac: str) -> bytes:
    """Parse ``aa:bb:cc:dd:ee:ff`` into six bytes."""
    parts = mac.replace("-", ":").split(":")
    if len(parts) != 6:
        raise DecodeError(f"not a MAC address: {mac!r}")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError as exc:
        raise DecodeError(f"not a MAC address: {mac!r}") from exc


def xor_key(mac: str) -> int:
    """Per-frame XOR key: sum of the six MAC bytes, low 8 bits."""
    return sum(mac_bytes(mac)) & 0xFF


def crc_params(mac: str) -> tuple[int, int]:
    """CRC16 ``(init, poly)`` derived from the MAC."""
    b = mac_bytes(mac)
    return (b[5] + b[4] + 10) & 0xFFFF, (((b[5] + 11) << 8) | b[4]) & 0xFFFF


def crc16(data: bytes, init: int, poly: int) -> int:
    """Reflected CRC16 (LSB-first) used by the device."""
    value = init & 0xFFFF
    for byte in data:
        value ^= byte & 0xFF
        for _ in range(8):
            value = ((value >> 1) ^ poly) if (value & 1) else (value >> 1)
            value &= 0xFFFF
    return value & 0xFFFF


def build_frame(payload: bytes, mac: str, kind: str = "v1") -> bytes:
    """Wrap ``payload`` into a wire-ready frame for ``mac``."""
    magic = {"v1": MAGIC_V1, "v2": MAGIC_V2, "v0": MAGIC_V0, "open": MAGIC_OPEN}[kind]
    body = bytes(payload)
    init, poly = crc_params(mac)
    checksum = crc16(body, init, poly)
    frame = (
        bytes(magic)
        + len(body).to_bytes(2, "big")
        + body
        + checksum.to_bytes(2, "big")
    )
    if kind == "open":
        return frame  # 0xF0 0x0F frames are not obfuscated
    key = xor_key(mac)
    return bytes(b ^ key for b in frame)


def parse_frame(data: bytes, mac: str) -> Frame:
    """Decode a wire frame, validating CRC.

    A magic-byte match alone is not sufficient: several XOR keys can produce a
    plausible magic (``0xA5 ^ 0x36 == 0x93``), so CRC validation is the gate.
    """
    raw = bytes(data)
    if len(raw) < 6:
        raise DecodeError(f"frame too short: {len(raw)} bytes")

    if raw[0] == MAGIC_OPEN[0] and raw[1] == MAGIC_OPEN[1]:
        length = int.from_bytes(raw[2:4], "big")
        if len(raw) < 6 + length:
            raise DecodeError("truncated open frame")
        return Frame(MAGIC_OPEN, raw[4 : 4 + length])

    key = xor_key(mac)
    decoded = bytes(b ^ key for b in raw)
    magic = (decoded[0], decoded[1])
    if magic not in (MAGIC_V1, MAGIC_V2, MAGIC_V0):
        raise DecodeError(f"unknown magic {magic[0]:02X} {magic[1]:02X}")

    length = int.from_bytes(decoded[2:4], "big")
    if len(decoded) < 6 + length:
        raise DecodeError(f"truncated frame: need {6 + length}, have {len(decoded)}")

    payload = decoded[4 : 4 + length]
    got = int.from_bytes(decoded[4 + length : 6 + length], "big")
    init, poly = crc_params(mac)
    want = crc16(payload, init, poly)
    if got != want:
        raise DecodeError(f"CRC mismatch: frame=0x{got:04X} computed=0x{want:04X}")
    return Frame(magic, payload)


def deframe_payload(payload: bytes, xor: int = PAYLOAD_XOR) -> bytes:
    """Remove the payload-level XOR, yielding plaintext."""
    return bytes(b ^ xor for b in payload)


def enframe_payload(plaintext: bytes, xor: int = PAYLOAD_XOR) -> bytes:
    """Apply the payload-level XOR (inverse of :func:`deframe_payload`)."""
    return bytes(b ^ xor for b in plaintext)


def parse_response(data: bytes, mac: str, payload_xor: int | None = None) -> Response:
    """Full decode of a device response: MAC XOR, CRC, payload XOR, checksum.

    ``payload_xor`` defaults to the value captured for this MAC (see
    :data:`KNOWN_PROFILES`). Passing the wrong constant still passes the frame
    CRC but yields garbage fields, so prefer :func:`profile_for` over guessing.
    """
    if payload_xor is None:
        payload_xor = profile_for(mac).payload_xor
    frame = parse_frame(data, mac)
    plain = deframe_payload(frame.payload, payload_xor)
    if len(plain) < 3:
        raise DecodeError("response too short")

    reply_type, length = plain[0], plain[1]
    if 2 + length >= len(plain) + 1:
        raise DecodeError(f"declared length {length} exceeds payload {len(plain)}")

    data_body = plain[2 : 2 + length]
    checksum = plain[2 + length]
    want = sum(plain[: 2 + length]) & 0xFF
    if checksum != want:
        raise DecodeError(f"checksum mismatch: frame=0x{checksum:02X} computed=0x{want:02X}")
    return Response(reply_type, length, data_body, checksum, plain)


def build_read(mac: str, read_payload: bytes | None = None) -> bytes:
    """Frame that requests a live reading.

    ``read_payload`` defaults to the value captured for this MAC. Sending
    another device's command is ignored by the firmware without any error.
    """
    if read_payload is None:
        read_payload = profile_for(mac).read_payload
    return build_frame(read_payload, mac)


def encode_threshold(watts: int) -> bytes:
    """Encode a threshold value into its two payload bytes."""
    wire = watts ^ THRESHOLD_MASK
    return bytes([(wire >> 8) & 0xFF, wire & 0xFF])


def decode_threshold(hi: int, lo: int) -> int:
    """Decode a threshold from its two payload bytes."""
    return ((hi << 8) | lo) ^ THRESHOLD_MASK


def build_set_threshold(mac: str, watts: int, tail: int | None = None) -> bytes:
    """Frame that sets the alarm threshold.

    The trailing byte is not derivable from the value (see
    :data:`CONFIG_TAIL_SAMPLES`); callers should pass a captured ``tail`` when
    they need byte-exact replay, otherwise a derived default is used.
    """
    if tail is None:
        tail = CONFIG_TAIL_SAMPLES.get(watts, 0x00)
    payload = (
        CMD_CONFIG_HEAD
        + encode_threshold(watts)
        + CMD_CONFIG_MID
        + bytes([tail])
    )
    return build_frame(payload, mac)


def build_clear(mac: str) -> bytes:
    """Frame that clears the device's cumulative counters."""
    return build_frame(CMD_CLEAR, mac)


def advertised_name(mac: str) -> str:
    """Advertised name for ``mac``: ``YUNM`` + the MAC bytes reversed, as hex."""
    b = mac_bytes(mac)
    return "YUNM" + "".join(f"{x:02X}" for x in reversed(b))


def mac_from_advertised_name(name: str) -> str | None:
    """Recover the MAC from an advertised name, or ``None`` if it is not ours."""
    if len(name) != 16 or not name.upper().startswith("YUNM"):
        return None
    hexpart = name[4:16].upper()
    try:
        raw = bytes.fromhex(hexpart)
    except ValueError:
        return None
    return ":".join(f"{b:02X}" for b in reversed(raw))


def frame_bytes_from_response(data: bytes) -> bytes:
    """Legacy helper kept for callers that need the raw wire bytes."""
    return bytes(data)
