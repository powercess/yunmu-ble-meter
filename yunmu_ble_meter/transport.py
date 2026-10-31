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
    ) -> None:
        self.mac = mac.upper()
        self.connect_timeout = connect_timeout
        self.notify_settle = notify_settle
        self.reply_timeout = reply_timeout

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
        """Refresh the cached object paths; return True when the device is known."""
        self._device_path = None
        self._notify_path = None
        self._write_path = None
        for path, ifaces in self._objects().items():
            dev = ifaces.get(DEV_IFACE)
            if dev and str(dev.get("Address", "")).upper() == self.mac:
                self._device_path = path
            char = ifaces.get(CHR_IFACE)
            if char:
                uuid = str(char.get("UUID", "")).lower()
                if uuid == NOTIFY_UUID:
                    self._notify_path = path
                elif uuid == WRITE_UUID:
                    self._write_path = path
        return self._device_path is not None

    def _pump(self, seconds: float) -> None:
        ctx = self._loop.get_context()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.005)

    # ------------------------------------------------------------- notify

    def _on_properties(self, _iface, changed, _invalidated, path=None) -> None:
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
            obj = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), DEV_IFACE)
            try:
                obj.Connect()
            except Exception:
                self._scan(scan_timeout)
                self._resolve()
                dev = self._objects()[self._device_path][DEV_IFACE]
                if not dev.get("Connected"):
                    raise
            self._pump(1.0)

        self._resolve()
        if not (self._notify_path and self._write_path):
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

    def _start_notify(self) -> None:
        if self._notify_started:
            return
        char = dbus.Interface(self._bus.get_object(BLUEZ, self._notify_path), CHR_IFACE)
        char.StartNotify()
        # The CCCD write must land before the first notification can flow, and
        # this handler holds the only reply, so this wait is mandatory.
        self._pump(self.notify_settle)
        self._notify_started = True

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
        try:
            self._write(frame)
        except Exception as exc:
            raise TransportError(f"write failed: {exc}") from exc

        deadline = time.monotonic() + (timeout or self.reply_timeout)
        while time.monotonic() < deadline:
            ctx = self._loop.get_context()
            ctx.iteration(False)
            if self._frames:
                return self._frames[0]
            time.sleep(0.005)
        raise TransportError("device did not reply")

    def read(self, timeout: float | None = None) -> Reading:
        """Perform one reading."""
        reply = self.transact(protocol.build_read(self.mac), timeout)
        resp = protocol.parse_response(reply, self.mac)
        return Reading(
            counter=resp.counter,
            voltage=resp.voltage or 0.0,
            power=resp.power or 0.0,
            current=resp.current or 0.0,
            power_factor=resp.power_factor or 0.0,
            threshold=resp.threshold or 0,
            raw=resp.plaintext,
        )

    def clear(self, timeout: float | None = None) -> protocol.Response:
        """Clear the device's cumulative counters; returns the device acknowledgement."""
        reply = self.transact(protocol.build_clear(self.mac), timeout)
        return protocol.parse_response(reply, self.mac)

    def set_threshold(self, watts: int, tail: int | None = None, timeout: float | None = None) -> protocol.Response:
        """Set the alarm threshold; returns the device acknowledgement."""
        reply = self.transact(protocol.build_set_threshold(self.mac, watts, tail), timeout)
        return protocol.parse_response(reply, self.mac)

    # ---------------------------------------------------------- teardown

    def close(self) -> None:
        """Release the link so the phone app can connect again."""
        try:
            if self._notify_path and self._notify_started:
                char = dbus.Interface(self._bus.get_object(BLUEZ, self._notify_path), CHR_IFACE)
                char.StopNotify()
        except Exception:
            pass
        self._notify_started = False
        try:
            if self._device_path:
                dev = dbus.Interface(self._bus.get_object(BLUEZ, self._device_path), DEV_IFACE)
                dev.Disconnect()
        except Exception:
            pass

    def __enter__(self) -> "BleMeter":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
