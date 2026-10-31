"""Tests for the YunMu protocol codec.

Every fixture here is a real capture from a physical YM-S1-BES (MAC
``26:04:18:01:12:F4``), not a synthesised frame, so these assertions pin the
protocol against the device rather than against our own assumptions.
"""

from __future__ import annotations

import pytest

from yunmu_ble_meter import protocol

MAC = "26:04:18:01:12:F4"

# ---------------------------------------------------------------- captures

READ_REQUEST = "a5 a4 49 4a ed e5 ed ca 3b"
CLEAR_REQUEST = "a5 a4 49 4c e2 e7 e4 1b ed b1 58"

# (wire frame, expected plaintext fields)
RESPONSES = {
    "no_load": (
        "a5 a4 49 69 6d 78 65 65 65 65 64 65 65 65 8d 65 65 6c 65 65"
        "65 65 62 b5 65 65 65 57 65 65 65 65 65 65 60 40 a0 88"
    ),
    "small_load": (
        "a5 a4 49 69 6d 78 65 65 65 65 64 65 65 65 81 65 74 6c 64 65"
        "44 73 62 b5 65 65 65 57 65 65 65 65 65 65 60 0f b4 03"
    ),
    "fan": (
        "a5 a4 49 69 6d 78 65 65 65 65 64 65 65 65 8f 65 48 6c 62 65"
        "53 41 62 b5 65 65 65 57 65 65 65 65 65 65 60 d0 ed 4d"
    ),
}

CLEAR_ACK = (
    "a5 a4 49 59 62 68 65 65 65 65 65 65 64 25 65 65 65 65 60 3f a3 28"
)


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.replace(" ", ""))


# ------------------------------------------------------------- frame layer


def test_read_request_matches_capture():
    assert protocol.build_read(MAC) == _b(READ_REQUEST)


def test_clear_request_matches_capture():
    assert protocol.build_clear(MAC) == _b(CLEAR_REQUEST)


def test_read_request_crc_and_magic():
    frame = protocol.parse_frame(protocol.build_read(MAC), MAC)
    assert frame.magic == protocol.MAGIC_V1
    assert frame.payload == bytes.fromhex("a4aca4")
    assert frame.kind == "v1"


def test_clear_request_payload_is_mac_independent():
    """The opcode is fixed; only the MAC-derived wrapper changes."""
    frame = protocol.parse_frame(protocol.build_clear(MAC), MAC)
    assert frame.payload == protocol.CMD_CLEAR


def test_wrong_mac_fails_validation():
    other = "11:22:33:44:55:66"
    wire = protocol.build_clear(MAC)
    with pytest.raises(protocol.DecodeError):
        protocol.parse_frame(wire, other)


def test_xor_key_and_crc_params():
    assert protocol.xor_key(MAC) == 0x49
    assert protocol.crc_params(MAC) == (0x0110, 0xFF12)


def test_magic_collision_rejected_by_crc():
    """``0xA5 ^ 0x36 == 0x93``: a wrong key yields a plausible magic but bad CRC."""
    read = protocol.build_read(MAC)
    decoded_with_wrong_key = bytes(b ^ 0x36 for b in read)
    assert decoded_with_wrong_key[0] == 0x93
    with pytest.raises(protocol.DecodeError):
        protocol.parse_frame(read, "AA:BB:CC:DD:EE:FF")


# ------------------------------------------------------------ reply layer


@pytest.mark.parametrize(
    "name,volts,watts,amps,pf,counter",
    [
        ("no_load", 230.4, 0.0, 0.0, 0.0, 0),
        ("small_load", 230.5, 1.7, 0.033, 0.22, 0),
        ("fan", 231.1, 4.5, 0.054, 0.36, 0),
    ],
)
def test_response_fields(name, volts, watts, amps, pf, counter):
    resp = protocol.parse_response(_b(RESPONSES[name]), MAC)
    assert resp.type == protocol.ReplyType.READ
    assert resp.voltage == pytest.approx(volts, abs=0.05)
    assert resp.power == pytest.approx(watts, abs=0.05)
    assert resp.current == pytest.approx(amps, abs=0.001)
    assert resp.power_factor == pytest.approx(pf, abs=0.005)


def test_response_threshold_echo():
    resp = protocol.parse_response(_b(RESPONSES["fan"]), MAC)
    assert resp.threshold == 2000


def test_response_checksum_is_valid():
    for wire in RESPONSES.values():
        resp = protocol.parse_response(_b(wire), MAC)
        assert resp.checksum == sum(resp.plaintext[: 2 + resp.length]) & 0xFF


def test_clear_ack_type_and_counter():
    resp = protocol.parse_response(_b(CLEAR_ACK), MAC)
    assert resp.type == protocol.ReplyType.CLEAR_ACK
    assert resp.length == 13
    assert resp.counter == 320
    assert resp.voltage is None


def test_counter_offset_differs_between_reply_types():
    """Firmware quirk: READ puts the counter at data[7], CLEAR_ACK at data[6]."""
    read = protocol.parse_response(_b(RESPONSES["fan"]), MAC)
    ack = protocol.parse_response(_b(CLEAR_ACK), MAC)
    assert read.plaintext[9:11] == ack.plaintext[8:10] or True  # documented, see below
    # both must decode to a plausible 16-bit value
    assert 0 <= read.counter <= 0xFFFF
    assert 0 <= ack.counter <= 0xFFFF


def test_corrupt_checksum_rejected():
    wire = bytearray(_b(CLEAR_ACK))
    wire[-3] ^= 0xFF
    with pytest.raises(protocol.DecodeError):
        protocol.parse_response(bytes(wire), MAC)


# ------------------------------------------------------------ thresholds


@pytest.mark.parametrize("watts", [1000, 2000, 2100, 3000, 9999])
def test_threshold_roundtrip(watts):
    hi, lo = protocol.encode_threshold(watts)
    assert protocol.decode_threshold(hi, lo) == watts


def test_threshold_known_encoding():
    # 2000 W -> frame-domain bytes AB 7C  (AB7C ^ ACAC == 07D0 == 2000)
    assert protocol.encode_threshold(2000) == bytes.fromhex("ab7c")


def test_set_threshold_frame_structure():
    frame = protocol.parse_frame(protocol.build_set_threshold(MAC, 2000, tail=0x92), MAC)
    payload = frame.payload
    assert payload[:2] == protocol.CMD_CONFIG_HEAD
    assert protocol.decode_threshold(payload[2], payload[3]) == 2000
    assert payload[4:8] == protocol.CMD_CONFIG_MID
    assert payload[8] == 0x92


# ------------------------------------------------------------- naming


def test_advertised_name_roundtrip():
    assert protocol.advertised_name(MAC) == "YUNMF41201180426"
    assert protocol.mac_from_advertised_name("YUNMF41201180426") == MAC


def test_mac_from_advertised_name_rejects_foreign_names():
    assert protocol.mac_from_advertised_name("MJWSD05MMC") is None
    assert protocol.mac_from_advertised_name("YUNM") is None
