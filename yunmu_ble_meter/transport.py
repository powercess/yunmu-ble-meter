"""BlueZ transport for YunMu BLE meters, over the system D-Bus.

Talks to ``org.bluez`` directly rather than through a BLE library, because these
devices stop advertising once connected and have no GATT cache entries when they
are idle — a scan-based client cannot find them, but the BlueZ object tree can.

Requires ``dbus-python`` (``python3-dbus`` on Debian) plus GLib; these come from
the distro packages, which is why this module is not runnable inside a plain
virtualenv.
"""

from __future__ import annotations

import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from . import protocol
from .transport_shim import Reading

__all__ = ["BleMeter", "DeviceNotFound", "TransportError", "Reading", "SERVICE_UUID", "NOTIFY_UUID", "WRITE_UUID"]

SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEV_IFACE = "org.bluez.Device1"
CHR_IFACE = "org.bluez.GattCharacteristic1"
ADAPTER_IFACE = "org.bluez.Adapter1"


class DeviceNotFound(Exception):
    """The target device is neither cached by BlueZ nor currently advertising."""


class TransportError(Exception):
    """A GATT operation failed."""


class BleMeter:
    """Connection to a single YunMu meter.

    The device accepts exactly one BLE central at a time and stops advertising
    while connected, so a long-lived collector naturally locks out the phone app.
    """

    def __init__(
        self,
        mac: str,
        *,
        connect_timeout: float = 20.0,
        notify_settle: float = 0.8,
        reply_timeout: float = 5.0,
        reconnect_backoff: float = 1.5,
        connect_retries: int = 3,
        disconnect_timeout: float = 5.0,
        disconnect_settle: float = 0.3,
        write_settle: float = 0.6,
        learn_payload_xor: bool = False,
    ) -> None:
        self.mac = mac.upper()
        #: per-device protocol parameters (read command + payload obfuscation)
        self.profile = protocol.profile_for(self.mac)
        self.connect_timeout = connect_timeout
        self.notify_settle = notify_settle
        self.reply_timeout = reply_timeout
        #: the meter aborts rapid reconnects, so attempts are spaced out
        self.reconnect_backoff = reconnect_backoff
        self.connect_retries = connect_retries
        #: the controller refuses the next link while a teardown is in flight
        self.disconnect_timeout = disconnect_timeout
        self.disconnect_settle = disconnect_settle
        #: link stabilisation granted before the first GATT write
        self.write_settle = write_settle
        #: recover an unknown payload de-obfuscation constant from the first reply
        self.learn_payload_xor = learn_payload_xor

        self._bus: dbus.SystemBus | None = None
        self._loop: GLib.MainLoop | None = None
        self._buffer = bytearray()
        self._frames: list[bytes] = []
        self._notify_started = False
        self._device_path: str | None = None
        self._notify_path: str | None = None
        self._write_path: str | None = None

    # ---------------------------------------------------------------- setup

    def _ensure_loop(self) -> None:
        if self._bus is None:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
            self._loop = GLib.MainLoop()

    def _objects(self) -> dict:
        om = dbus.Interface(self._bus.get_object(BLUEZ, "/"), OM_IFACE)
        return om.GetManagedObjects()

    def _adapter_path(self) -> str:
        for path, ifaces in self._objects().items():
            if ADAPTER_IFACE in ifaces:
                return path
        return "/org/bluez/hci0"

    def _resolve(self) -> bool:
        """Refresh the cached object paths; return True when the device is known.

        Characteristics are matched only within this device's own object tree:
        every meter exposes the same vendor UUIDs, so a global UUID scan would
        pick up a neighbouring device's characteristic and write to the wrong
        meter.
        """
        self._device_path = None
        self._notify_path = None
        self._write_path = None
        objs = self._objects()
        for path, ifaces in objs.items():
            dev = ifaces.get(DEV_IFACE)
            if dev and str(dev.get("Address", "")).upper() == self.mac:
                self._device_path = path
        if self._device_path is None:
            return False
        prefix = self._device_path + "/"
        for path, ifaces in objs.items():
            if not path.startswith(prefix):
                continue
            char = ifaces.get(CHR_IFACE)
            if not char:
                continue
            uuid = str(char.get("UUID", "")).lower()
            if uuid == NOTIFY_UUID:
                self._notify_path = path
            elif uuid == WRITE_UUID:
                self._write_path = path
        return True

    def _pump(self, seconds: float) -> None:
        ctx = self._loop.get_context()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.005)

    # ------------------------------------------------------------- notify

    def _on_properties(self, _iface, changed, _invalidated, path=None) -> None:
        # Only react to our own device. Without this filter every meter BlueZ
        # has connected subscribes to the same PropertiesChanged signal, so a
        # neighbouring meter's notifications land in our frame buffer and
        # corrupt it (their frames fail our MAC-derived CRC and get discarded,
        # but the bytes still consume buffer space and race the real reply).
        if path is not None and self._notify_path is not None and path != self._notify_path:
            return
        value = changed.get("Value")
        if value is None:
            return
        self._buffer.extend(bytes(value))
        self._drain_buffer()

    def _drain_buffer(self) -> None:
        """Cut complete frames out of the byte stream.

        Several XOR keys can produce a plausible magic byte pair, so a frame is
        only accepted once its CRC verifies. Crucially, a *candidate* frame that
        is merely incomplete must not cause the buffer to be sacrificed — the
        device delivers a reply as two notifications (20 B + rest), so the tail
        is legitimately missing for a moment.
        """
        while len(self._buffer) >= 6:
            consumed = 0
            awaiting = False
            for candidate in range(256):
                head = [b ^ candidate for b in self._buffer[:6]]
                if (head[0], head[1]) not in (
                    protocol.MAGIC_V1,
                    protocol.MAGIC_V2,
                    protocol.MAGIC_V0,
                ):
                    continue
                length = (head[2] << 8) | head[3]
                if length > 512:
                    continue  # implausible length: not a real frame header
                if 6 + length > len(self._buffer):
                    awaiting = True  # plausible header, tail still in flight
                    continue
                frame = bytes(self._buffer[: 6 + length])
                try:
                    protocol.parse_frame(frame, self.mac)
                except protocol.DecodeError:
                    continue
                self._frames.append(frame)
                consumed = 6 + length
                break
            if consumed:
                del self._buffer[:consumed]
                continue
            if awaiting:
                return  # keep the partial frame, wait for the rest
            # No candidate header at all: this byte cannot start a frame.
            if len(self._buffer) > 128:
                self._buffer.clear()
                return
            del self._buffer[:1]

    # ----------------------------------------------------------- connect

    def connect(self, scan_timeout: float = 30.0) -> None:
        """Connect and subscribe to notifications.

        Raises :class:`DeviceNotFound` when the device neither is cached nor
        advertises within ``scan_timeout``. These devices only advertise for a
        short window after power-up, so a powered-off or already-connected meter
        simply cannot be reached.
        """
        self._ensure_loop()
        if not self._resolve():
            self._scan(scan_timeout)
            if not self._resolve():
                raise DeviceNotFound(self.mac)

        dev = self._objects()[self._device_path][DEV_IFACE]
        if not dev.get("Connected"):
            # These meters reject back-to-back connect attempts with
            # "le-connection-abort-by-local", so a failed attempt is retried
            # after a growing delay instead of being surfaced immediately.
            last_exc: Exception | None = None
            for attempt in range(self.connect_retries):
                try:
                    obj = dbus.Interface(
                        self._bus.get_object(BLUEZ, self._device_path), DEV_IFACE
                    )
                    obj.Connect()
                except Exception as exc:
                    last_exc = exc
                    self._pump(self.reconnect_backoff * (attempt + 1))
                    self._resolve()
                    if self._device_path is None:
                        self._scan(scan_timeout)
                        if not self._resolve():
                            continue
                    continue
                if self._wait_connected():
                    break
                last_exc = TransportError("connected flag never set")
                self._pump(self.reconnect_backoff * (attempt + 1))
            else:
                raise TransportError(f"could not connect to {self.mac}: {last_exc}")

        self._resolve()
        if not (self._notify_path and self._write_path):
            # Service discovery runs after the link is up; on a freshly
            # established connection the characteristics are not in the object
            # tree yet.
            deadline = time.monotonic() + self.connect_timeout
            while time.monotonic() < deadline:
                self._pump(0.2)
                self._resolve()
                if self._notify_path and self._write_path:
                    break
            else:
                raise TransportError("GATT characteristics not exposed by the device")

        self._bus.add_signal_receiver(
            self._on_properties,
            dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged",
            path_keyword="path",
        )
        # _start_notify already waits for the CCCD write to settle; adding a
        # blanket connect_timeout here made the first poll take 20+ s.
        self._start_notify()

    def _write_ready(self) -> bool:
        """True once BlueZ will accept a GATT write on this link.

        ``Connected`` and ``ServicesResolved`` can both be true while the LE
        link is still coming up; a write issued then fails with "Failed to
        initiate write". There is no readiness property, so callers probe by
        writing (the read command is idempotent) and retry.
        """
        props = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), PROPS_IFACE)
        try:
            return bool(props.Get(DEV_IFACE, "Connected")) and bool(
                props.Get(DEV_IFACE, "ServicesResolved")
            )
        except Exception:
            return False

    def _wait_connected(self, timeout: float | None = None) -> bool:
        """Block until BlueZ reports this device connected.

        ``Connect()`` is asynchronous from LE's point of view: it resolves once
        the request is accepted, not when the link (and its encryption) is up.
        """
        limit = timeout if timeout is not None else self.connect_timeout
        deadline = time.monotonic() + limit
        props = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), PROPS_IFACE)
        while time.monotonic() < deadline:
            try:
                if props.Get(DEV_IFACE, "Connected"):
                    return True
            except Exception:
                pass
            self._pump(0.1)
        return False

    def _start_notify(self) -> None:
        if self._notify_started:
            return
        char = dbus.Interface(self._bus.get_object(BLUEZ, self._notify_path), CHR_IFACE)
        # BlueZ refuses to register a second notify session while the previous
        # CCCD subscription is still being torn down, which happens when a
        # collector reconnects quickly. Retry rather than fail the cycle.
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                char.StartNotify()
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                self._pump(0.5 * (attempt + 1))
        if last_exc is not None:
            raise TransportError(f"notify subscription failed: {last_exc}") from last_exc
        # The CCCD write must land before the first notification can flow, and
        # this handler holds the only reply, so this wait is mandatory.
        self._pump(self.notify_settle)
        self._notify_started = True

    def _resubscribe(self) -> None:
        """Re-establish the link and the notify subscription after a drop."""
        self._notify_started = False
        try:
            self._resolve()
            if not self._device_path:
                if not self._resolve():
                    self._scan(10.0)
                if not self._resolve():
                    return
            dev = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), DEV_IFACE)
            dev.Connect()
        except Exception:
            return
        self._wait_connected(5.0)
        self._resolve()
        try:
            self._start_notify()
        except Exception:
            pass

    def _scan(self, seconds: float) -> None:
        adapter = dbus.Interface(self._bus.get_object(BLUEZ, self._adapter_path()), ADAPTER_IFACE)
        try:
            adapter.SetDiscoveryFilter({"Transport": dbus.String("le")})
        except Exception:
            pass
        try:
            adapter.StartDiscovery()
        except Exception:
            pass
        self._pump(seconds)
        try:
            adapter.StopDiscovery()
        except Exception:
            pass

    # -------------------------------------------------------------- io

    def _write(self, frame: bytes) -> None:
        char = dbus.Interface(self._bus.get_object(BLUEZ, self._write_path), CHR_IFACE)
        char.WriteValue(
            dbus.Array(frame, signature="y"),
            dbus.Dictionary({}, signature="sv"),
        )

    def transact(self, frame: bytes, timeout: float | None = None) -> bytes:
        """Send ``frame`` and wait for one complete reply frame."""
        if not (self._notify_path and self._write_path):
            self._resolve()
        if not (self._notify_path and self._write_path):
            raise TransportError("not connected")
        self._start_notify()

        self._frames.clear()
        self._buffer.clear()
        # A write can fail with "Failed to initiate write" / "Not connected"
        # while the LE link is still settling, especially right after another
        # meter's link was torn down. The read command is idempotent, so a
        # failed attempt is simply retried after confirming the link state.
        last_exc: Exception | None = None
        for attempt in range(4):
            deadline = time.monotonic() + self.write_settle
            while time.monotonic() < deadline and not self._write_ready():
                self._pump(0.05)
            try:
                self._write(frame)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if "Not connected" in str(exc):
                    if not self._wait_connected(5.0):
                        self._resubscribe()
                        if not self._wait_connected(5.0):
                            break
                self._pump(0.3 * (attempt + 1))
        if last_exc is not None:
            raise TransportError(f"write failed: {last_exc}") from last_exc

        deadline = time.monotonic() + (timeout or self.reply_timeout)
        while time.monotonic() < deadline:
            ctx = self._loop.get_context()
            ctx.iteration(False)
            if self._frames:
                return self._frames[0]
            time.sleep(0.005)
        raise TransportError("device did not reply")

    def read(self, timeout: float | None = None) -> Reading:
        """Perform one reading.

        Uses this device's captured read command and payload obfuscation
        constant. Both are per-device values issued by the vendor cloud; if the
        device has no captured profile the generic defaults are used and the
        meter will most likely ignore the request silently.
        """
        reply = self.transact(protocol.build_read(self.mac, self.profile.read_payload), timeout)
        if self.learn_payload_xor and not self.profile.observed_payload_xor:
            self._learn_from(reply)
        resp = protocol.parse_response(reply, self.mac, self.profile.payload_xor)
        return Reading(
            counter=resp.counter,
            device_energy_kwh=resp.device_energy_kwh,
            voltage=resp.voltage or 0.0,
            power=resp.power or 0.0,
            current=resp.current or 0.0,
            power_factor=resp.power_factor or 0.0,
            threshold=resp.threshold or 0,
            raw=resp.plaintext,
        )

    def _learn_from(self, reply: bytes) -> None:
        """Infer the payload constant from a real reply and cache it for this MAC."""
        frame = protocol.parse_frame(reply, self.mac)
        guess = protocol.guess_payload_xor(frame.payload)
        if guess is None:
            raise protocol.DecodeError(
                "could not infer payload_xor from the reply; capture it with "
                "tools/fake_meter and add it to KNOWN_PROFILES"
            )
        self.profile = protocol.DeviceProfile(
            mac=self.mac,
            read_payload=self.profile.read_payload,
            payload_xor=guess,
            observed_read_payload=self.profile.observed_read_payload,
            observed_payload_xor=True,
        )
        protocol.KNOWN_PROFILES[self.mac] = self.profile
        print(f"{self.mac}: learned payload_xor=0x{guess:02X}", flush=True)

    def clear(self, timeout: float | None = None) -> protocol.Response:
        """Send the counter-clear command; returns the device acknowledgement.

        The device answers with a ``0x07`` reply echoing the counter value at the
        moment of the request, which proves the command was accepted. It was
        **not** observed to reset the counter readable in a subsequent ``read()``
        during testing: replaying the exact frame captured from the app produced
        the acknowledgement but left ``counter`` unchanged. Treat this as
        "command accepted", not "state verified reset" — see README.
        """
        reply = self.transact(protocol.build_clear(self.mac), timeout)
        return protocol.parse_response(reply, self.mac, self.profile.payload_xor)

    def set_threshold(self, watts: int, tail: int | None = None, timeout: float | None = None) -> protocol.Response:
        """Set the alarm threshold; returns the device acknowledgement."""
        reply = self.transact(protocol.build_set_threshold(self.mac, watts, tail), timeout)
        return protocol.parse_response(reply, self.mac, self.profile.payload_xor)

    # ---------------------------------------------------------- teardown

    def close(self) -> None:
        """Release the link so the phone app can connect again.

        Waits for BlueZ to finish tearing the link down: the controller will
        refuse to bring up the next device while the previous disconnect is
        still in flight, which shows up as "Failed to initiate write" or
        "le-connection-abort-by-local" on the following connection.
        """
        try:
            if self._notify_path and self._notify_started:
                char = dbus.Interface(self._bus.get_object(BLUEZ, self._notify_path), CHR_IFACE)
                char.StopNotify()
        except Exception:
            pass
        self._notify_started = False
        if not self._device_path:
            return
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ, self._device_path), PROPS_IFACE
            )
            dev = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), DEV_IFACE)
            dev.Disconnect()
            deadline = time.monotonic() + self.disconnect_timeout
            while time.monotonic() < deadline:
                try:
                    if not props.Get(DEV_IFACE, "Connected"):
                        break
                except Exception:
                    break
                self._pump(0.05)
            # the controller needs a moment after the link is gone
            self._pump(self.disconnect_settle)
        except Exception:
            pass
        self._device_path = None
        self._notify_path = None
        self._write_path = None

    def __enter__(self) -> "BleMeter":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
