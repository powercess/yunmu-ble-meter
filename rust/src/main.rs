//! Lightweight YunMu BLE meter collector for edge devices.
//!
//! Replaces the Python service with a single static binary: direct BlueZ D-Bus
//! client, hand-written Prometheus endpoint, no runtime dependencies.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::process::exit;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;
use tokio::time::{sleep, timeout};
use zbus::message::Type as MessageType;
use zbus::zvariant::{OwnedObjectPath, OwnedValue};
use zbus::{Connection, MatchRule, MessageStream, Proxy};

const NOTIFY_UUID: &str = "49535343-1e4d-4bd9-ba61-23c647249616";
const WRITE_UUID: &str = "49535343-8841-43f4-a8d4-ecbe34729bb3";
const BLUEZ: &str = "org.bluez";
const DEV_IFACE: &str = "org.bluez.Device1";
const CHR_IFACE: &str = "org.bluez.GattCharacteristic1";
const ADAPTER_IFACE: &str = "org.bluez.Adapter1";
const PROPS_IFACE: &str = "org.freedesktop.DBus.Properties";

const MAGIC_V0: (u8, u8) = (0x93, 0x92);
const MAGIC_V1: (u8, u8) = (0xEC, 0xED);
const MAGIC_V2: (u8, u8) = (0xCE, 0xDE);
const WH_PER_COUNT: f64 = 0.26;

// ---------------------------------------------------------------- protocol

fn mac_bytes(mac: &str) -> Option<[u8; 6]> {
    let mut out = [0u8; 6];
    let mut n = 0;
    for part in mac.split(':') {
        if n == 6 {
            return None;
        }
        out[n] = u8::from_str_radix(part, 16).ok()?;
        n += 1;
    }
    if n == 6 {
        Some(out)
    } else {
        None
    }
}

fn xor_key(mac: &str) -> u8 {
    mac_bytes(mac).map(|b| b.iter().fold(0u8, |a, x| a.wrapping_add(*x))).unwrap_or(0)
}

fn crc_params(mac: &str) -> (u16, u16) {
    let b = mac_bytes(mac).unwrap_or([0; 6]);
    (
        (b[5] as u16 + b[4] as u16 + 10) & 0xFFFF,
        (((b[5] as u16 + 11) << 8) | b[4] as u16) & 0xFFFF,
    )
}

fn crc16(data: &[u8], init: u16, poly: u16) -> u16 {
    let mut v = init;
    for &byte in data {
        v ^= byte as u16;
        for _ in 0..8 {
            v = if v & 1 != 0 { (v >> 1) ^ poly } else { v >> 1 };
        }
    }
    v & 0xFFFF
}

fn build_frame(mac: &str, payload: &[u8]) -> Vec<u8> {
    let (init, poly) = crc_params(mac);
    let crc = crc16(payload, init, poly);
    let mut frame = Vec::with_capacity(6 + payload.len());
    frame.extend_from_slice(&[MAGIC_V1.0, MAGIC_V1.1]);
    frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    frame.extend_from_slice(payload);
    frame.extend_from_slice(&crc.to_be_bytes());
    let key = xor_key(mac);
    for b in frame.iter_mut() {
        *b ^= key;
    }
    frame
}

fn decode_frame(raw: &[u8], mac: &str) -> Option<Vec<u8>> {
    if raw.len() < 6 {
        return None;
    }
    let key = xor_key(mac);
    let decoded: Vec<u8> = raw.iter().map(|b| b ^ key).collect();
    let magic = (decoded[0], decoded[1]);
    if magic != MAGIC_V0 && magic != MAGIC_V1 && magic != MAGIC_V2 {
        return None;
    }
    let len = ((decoded[2] as usize) << 8) | decoded[3] as usize;
    if decoded.len() < 6 + len {
        return None;
    }
    let payload = decoded[4..4 + len].to_vec();
    let got = ((decoded[4 + len] as u16) << 8) | decoded[5 + len] as u16;
    let (init, poly) = crc_params(mac);
    if crc16(&payload, init, poly) != got {
        return None;
    }
    Some(payload)
}

struct Reading {
    counter: u16,
    voltage: f64,
    power: f64,
    current: f64,
    power_factor: f64,
    threshold: u16,
}

fn parse_response(payload: &[u8], xor: u8) -> Option<Reading> {
    let plain: Vec<u8> = payload.iter().map(|b| b ^ xor).collect();
    if plain.len() < 3 {
        return None;
    }
    let len = plain[1] as usize;
    if 2 + len >= plain.len() {
        return None;
    }
    let data = &plain[2..2 + len];
    let checksum = plain[2 + len];
    let want = plain[..2 + len].iter().fold(0u8, |a, b| a.wrapping_add(*b));
    if checksum != want || plain[0] != 0x08 || data.len() < 18 {
        return None;
    }
    let u16_at = |i: usize| -> u16 { ((data[i] as u16) << 8) | data[i + 1] as u16 };
    Some(Reading {
        counter: u16_at(7),
        voltage: u16_at(11) as f64 / 10.0,
        power: u16_at(9) as f64 / 10.0,
        current: u16_at(13) as f64 / 1000.0,
        power_factor: data[15] as f64 / 100.0,
        threshold: u16_at(16),
    })
}

// ------------------------------------------------------------------ config

#[derive(Clone)]
struct ConfigEntry {
    mac: String,
    name: String,
    payload: [u8; 3],
    xor: u8,
}

fn known_profile(mac: &str) -> Option<([u8; 3], u8)> {
    match mac {
        "26:04:18:01:12:F4" => Some(([0xA4, 0xAC, 0xA4], 0x2C)),
        "26:01:15:04:2F:96" => Some(([0x5A, 0x52, 0x5A], 0x0D)),
        "26:04:18:01:03:11" => Some(([0xD0, 0xD8, 0xD0], 0x79)),
        "26:04:18:01:03:16" => Some(([0xB0, 0xB8, 0xB0], 0x3F)),
        _ => None,
    }
}

fn parse_hex_payload(text: &str) -> Option<[u8; 3]> {
    let clean: String = text.chars().filter(|c| c.is_ascii_hexdigit()).collect();
    if clean.len() != 6 {
        return None;
    }
    let mut out = [0u8; 3];
    for i in 0..3 {
        out[i] = u8::from_str_radix(&clean[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

fn parse_int(text: &str) -> Option<u64> {
    let text = text.trim();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        u64::from_str_radix(hex, 16).ok()
    } else {
        text.parse().ok()
    }
}

/// Tiny TOML subset: `[[meter]]` tables with string/integer values.
fn load_config(path: &str) -> Vec<ConfigEntry> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("yunmu-meter: cannot read {path}: {e}");
            exit(2);
        }
    };
    let mut entries: Vec<ConfigEntry> = Vec::new();
    let mut cur: Option<ConfigEntry> = None;
    for raw in text.lines() {
        let line = raw.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        if line == "[[meter]]" {
            if let Some(e) = cur.take() {
                entries.push(e);
            }
            cur = Some(ConfigEntry {
                mac: String::new(),
                name: String::new(),
                payload: [0; 3],
                xor: 0,
            });
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        let value = value.trim();
        let Some(entry) = cur.as_mut() else { continue };
        match key {
            "mac" => entry.mac = value.trim_matches('"').to_uppercase(),
            "name" => entry.name = value.trim_matches('"').to_string(),
            "read_payload" => {
                if let Some(p) = parse_hex_payload(value.trim_matches('"')) {
                    entry.payload = p;
                }
            }
            "payload_xor" => {
                if let Some(v) = parse_int(value) {
                    entry.xor = v as u8;
                }
            }
            _ => {}
        }
    }
    if let Some(e) = cur {
        entries.push(e);
    }
    for entry in entries.iter_mut() {
        if entry.mac.is_empty() {
            eprintln!("yunmu-meter: config entry without mac");
            exit(2);
        }
        if let Some((payload, xor)) = known_profile(&entry.mac) {
            if entry.payload == [0; 3] {
                entry.payload = payload;
            }
            if entry.xor == 0 {
                entry.xor = xor;
            }
        }
        if entry.name.is_empty() {
            entry.name = entry.mac.clone();
        }
    }
    entries
}

// ------------------------------------------------------------------ metrics

#[derive(Clone, Default)]
struct Values {
    voltage: f64,
    current: f64,
    power: f64,
    power_factor: f64,
    threshold: u16,
    counter: u16,
    energy_device: f64,
}

struct DevState {
    mac: String,
    name: String,
    values: Values,
    last_ts: f64,
    ok: f64,
    duration: f64,
    error: String,
}

struct Shared {
    devices: Vec<DevState>,
}

fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn render_metrics(shared: &Shared) -> String {
    let now = now_epoch();
    let mut out = String::with_capacity(2048);
    out.push_str("# HELP yunmu_voltage_volts Line voltage in volts.\n# TYPE yunmu_voltage_volts gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_voltage_volts{{device=\"{}\"}} {:.1}\n",
            d.name, d.values.voltage
        ));
    }
    out.push_str("# HELP yunmu_current_amperes Load current in amperes.\n# TYPE yunmu_current_amperes gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_current_amperes{{device=\"{}\"}} {:.3}\n",
            d.name, d.values.current
        ));
    }
    out.push_str("# HELP yunmu_power_watts Active power in watts.\n# TYPE yunmu_power_watts gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_power_watts{{device=\"{}\"}} {:.1}\n",
            d.name, d.values.power
        ));
    }
    out.push_str("# HELP yunmu_power_factor Power factor (0..1).\n# TYPE yunmu_power_factor gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_power_factor{{device=\"{}\"}} {:.2}\n",
            d.name, d.values.power_factor
        ));
    }
    out.push_str("# HELP yunmu_threshold_watts Configured alarm threshold in watts.\n# TYPE yunmu_threshold_watts gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_threshold_watts{{device=\"{}\"}} {}\n",
            d.name, d.values.threshold
        ));
    }
    out.push_str("# HELP yunmu_counter_total Device energy accumulator.\n# TYPE yunmu_counter_total gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_counter_total{{device=\"{}\"}} {}\n",
            d.name, d.values.counter
        ));
    }
    out.push_str("# HELP yunmu_energy_device_kwh Device-reported cumulative energy.\n# TYPE yunmu_energy_device_kwh gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_energy_device_kwh{{device=\"{}\"}} {:.6}\n",
            d.name, d.values.energy_device
        ));
    }
    out.push_str("# HELP yunmu_scrape_success Whether the last read succeeded.\n# TYPE yunmu_scrape_success gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_scrape_success{{device=\"{}\"}} {:.0}\n",
            d.name, d.ok
        ));
    }
    out.push_str("# HELP yunmu_last_read_timestamp_seconds Unix timestamp of the last successful read.\n# TYPE yunmu_last_read_timestamp_seconds gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_last_read_timestamp_seconds{{device=\"{}\"}} {:.3}\n",
            d.name, d.last_ts
        ));
    }
    out.push_str("# HELP yunmu_sample_age_seconds Seconds since the last successful read.\n# TYPE yunmu_sample_age_seconds gauge\n");
    for d in &shared.devices {
        let age = if d.last_ts > 0.0 { now - d.last_ts } else { 0.0 };
        out.push_str(&format!(
            "yunmu_sample_age_seconds{{device=\"{}\"}} {:.3}\n",
            d.name, age
        ));
    }
    out.push_str("# HELP yunmu_read_duration_seconds Duration of the most recent read.\n# TYPE yunmu_read_duration_seconds gauge\n");
    for d in &shared.devices {
        out.push_str(&format!(
            "yunmu_read_duration_seconds{{device=\"{}\"}} {:.3}\n",
            d.name, d.duration
        ));
    }
    out
}

fn render_state_json(shared: &Shared) -> String {
    let mut out = String::from("[");
    for (i, d) in shared.devices.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&format!(
            "{{\"name\":\"{}\",\"mac\":\"{}\",\"voltage\":{:.1},\"current\":{:.3},\"power\":{:.1},\"ok\":{:.0},\"last_ts\":{:.3},\"error\":\"{}\"}}",
            d.name, d.mac, d.values.voltage, d.values.current, d.values.power, d.ok, d.last_ts,
            d.error.replace('"', "'")
        ));
    }
    out.push(']');
    out
}

fn http_loop(listener: TcpListener, shared: Arc<Mutex<Shared>>) {
    for stream in listener.incoming() {
        let mut stream = match stream {
            Ok(s) => s,
            Err(_) => continue,
        };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let mut buf = [0u8; 1024];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        let path = req.split_whitespace().nth(1).unwrap_or("/");
        let (status, ctype, body) = match path {
            "/metrics" => (
                "200 OK",
                "text/plain; version=0.0.4",
                render_metrics(&shared.lock().unwrap()),
            ),
            "/api/state" => (
                "200 OK",
                "application/json",
                render_state_json(&shared.lock().unwrap()),
            ),
            _ => ("200 OK", "text/plain", String::from("yunmu-meter\n")),
        };
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
    }
}

// --------------------------------------------------------------------- BLE

struct Meter {
    mac: String,
    payload: [u8; 3],
    xor: u8,
    conn: Option<Connection>,
    dev_path: Option<OwnedObjectPath>,
    notify_path: Option<OwnedObjectPath>,
    write_path: Option<OwnedObjectPath>,
    stream: Option<MessageStream>,
    notify_started: bool,
    ready: bool,
    buffer: Vec<u8>,
    failures: u32,
    offline: bool,
    skip_until: Instant,
    pub_state_index: usize,
}

impl Meter {
    fn new(entry: &ConfigEntry, index: usize) -> Self {
        Self {
            mac: entry.mac.clone(),
            payload: entry.payload,
            xor: entry.xor,
            conn: None,
            dev_path: None,
            notify_path: None,
            write_path: None,
            stream: None,
            notify_started: false,
            ready: false,
            buffer: Vec::with_capacity(256),
            failures: 0,
            offline: true,
            skip_until: Instant::now(),
            pub_state_index: index,
        }
    }

    async fn ensure_conn(&mut self) -> Result<(), String> {
        if self.conn.is_none() {
            let conn = timeout(Duration::from_secs(5), Connection::system())
                .await
                .map_err(|_| "dbus connect timeout".to_string())?
                .map_err(|e| e.to_string())?;
            self.conn = Some(conn);
        }
        Ok(())
    }

    fn conn(&self) -> Result<&Connection, String> {
        self.conn.as_ref().ok_or_else(|| "no dbus connection".to_string())
    }

    async fn managed_objects(
        &self,
    ) -> Result<HashMap<OwnedObjectPath, HashMap<String, HashMap<String, OwnedValue>>>, String> {
        let conn = self.conn()?;
        let om = Proxy::new(conn, BLUEZ, "/", "org.freedesktop.DBus.ObjectManager")
            .await
            .map_err(|e| e.to_string())?;
        let objs: HashMap<OwnedObjectPath, HashMap<String, HashMap<String, OwnedValue>>> =
            timeout(Duration::from_secs(5), om.call("GetManagedObjects", &()))
                .await
                .map_err(|_| "GetManagedObjects timeout".to_string())?
                .map_err(|e| e.to_string())?;
        Ok(objs)
    }

    async fn resolve(&mut self) -> Result<bool, String> {
        let objs = self.managed_objects().await?;
        self.dev_path = None;
        self.notify_path = None;
        self.write_path = None;
        for (path, ifaces) in objs.iter() {
            if let Some(dev) = ifaces.get(DEV_IFACE) {
                if let Some(addr) = dev.get("Address").and_then(value_string) {
                    if addr.eq_ignore_ascii_case(&self.mac) {
                        self.dev_path = Some(path.clone());
                    }
                }
            }
        }
        let Some(dev_path) = self.dev_path.clone() else {
            return Ok(false);
        };
        let prefix = format!("{}/", dev_path.as_str());
        for (path, ifaces) in objs.iter() {
            if !path.as_str().starts_with(&prefix) {
                continue;
            }
            let Some(chr) = ifaces.get(CHR_IFACE) else { continue };
            let Some(uuid) = chr.get("UUID").and_then(value_string) else {
                continue;
            };
            let uuid = uuid.to_lowercase();
            if uuid == NOTIFY_UUID {
                self.notify_path = Some(path.clone());
            } else if uuid == WRITE_UUID {
                self.write_path = Some(path.clone());
            }
        }
        Ok(self.notify_path.is_some() && self.write_path.is_some())
    }

    async fn scan(&self, seconds: u64) -> Result<(), String> {
        let objs = self.managed_objects().await?;
        let mut adapter_path = None;
        for (path, ifaces) in objs.iter() {
            if ifaces.contains_key(ADAPTER_IFACE) {
                adapter_path = Some(path.clone());
                break;
            }
        }
        let Some(adapter_path) = adapter_path else {
            return Ok(());
        };
        let conn = self.conn()?;
        let adapter = Proxy::new(conn, BLUEZ, adapter_path.as_str(), ADAPTER_IFACE)
            .await
            .map_err(|e| e.to_string())?;
        let _ = timeout(
            Duration::from_secs(2),
            adapter.call::<_, _, ()>("StartDiscovery", &()),
        )
        .await;
        sleep(Duration::from_secs(seconds)).await;
        let _ = timeout(
            Duration::from_secs(2),
            adapter.call::<_, _, ()>("StopDiscovery", &()),
        )
        .await;
        Ok(())
    }

    async fn connect(&mut self) -> Result<(), String> {
        self.ensure_conn().await?;
        if !self.resolve().await? {
            self.scan(30).await?;
            if !self.resolve().await? {
                return Err("device not found".to_string());
            }
        }
        let dev_path = self.dev_path.clone().ok_or("no device path")?;
        let conn = self.conn()?.clone();
        let dev = Proxy::new(&conn, BLUEZ, dev_path.as_str(), DEV_IFACE)
            .await
            .map_err(|e| e.to_string())?;
        let connected = self.get_bool(DEV_IFACE, "Connected").await.unwrap_or(false);
        if !connected {
            let mut last_err = None;
            let mut ok = false;
            for attempt in 0..3u64 {
                let call = timeout(Duration::from_secs(8), dev.call::<_, _, ()>("Connect", &())).await;
                match call {
                    Ok(Ok(_)) => {}
                    Ok(Err(e)) => last_err = Some(e.to_string()),
                    Err(_) => last_err = Some("Connect timeout".to_string()),
                }
                if self.wait_connected(20).await {
                    ok = true;
                    break;
                }
                sleep(Duration::from_millis(1500 * (attempt + 1))).await;
            }
            if !ok {
                return Err(format!("connect failed: {}", last_err.unwrap_or_default()));
            }
        }
        // characteristics appear after service discovery
        let deadline = Instant::now() + Duration::from_secs(20);
        while Instant::now() < deadline {
            self.resolve().await?;
            if self.notify_path.is_some() && self.write_path.is_some() {
                break;
            }
            sleep(Duration::from_millis(200)).await;
        }
        if self.notify_path.is_none() || self.write_path.is_none() {
            return Err("GATT characteristics not exposed".to_string());
        }
        self.start_notify().await?;
        self.ready = true;
        Ok(())
    }

    async fn get_bool(&self, iface: &str, prop: &str) -> Result<bool, String> {
        let conn = self.conn()?;
        let path = self.dev_path.as_ref().ok_or("no device path")?;
        let props = Proxy::new(conn, BLUEZ, path.as_str(), PROPS_IFACE)
            .await
            .map_err(|e| e.to_string())?;
        let value: OwnedValue = timeout(
            Duration::from_secs(5),
            props.call::<_, _, OwnedValue>("Get", &(iface, prop)),
        )
        .await
        .map_err(|_| "property get timeout".to_string())?
        .map_err(|e| e.to_string())?;
        bool::try_from(value).map_err(|_| "property not a bool".to_string())
    }

    async fn wait_connected(&self, secs: u64) -> bool {
        let deadline = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < deadline {
            if self.get_bool(DEV_IFACE, "Connected").await.unwrap_or(false)
                && self
                    .get_bool(DEV_IFACE, "ServicesResolved")
                    .await
                    .unwrap_or(false)
            {
                return true;
            }
            sleep(Duration::from_millis(100)).await;
        }
        false
    }

    async fn start_notify(&mut self) -> Result<(), String> {
        if self.notify_started {
            return Ok(());
        }
        let conn = self.conn()?.clone();
        let path = self.notify_path.clone().ok_or("no notify path")?;
        let chr = Proxy::new(&conn, BLUEZ, path.as_str(), CHR_IFACE)
            .await
            .map_err(|e| e.to_string())?;
        let mut last = String::new();
        for attempt in 0..4u64 {
            match timeout(Duration::from_secs(5), chr.call::<_, _, ()>("StartNotify", &())).await {
                Ok(Ok(_)) => {
                    last.clear();
                    break;
                }
                Ok(Err(e)) => last = e.to_string(),
                Err(_) => last = "StartNotify timeout".to_string(),
            }
            sleep(Duration::from_millis(500 * (attempt + 1))).await;
        }
        if !last.is_empty() {
            return Err(format!("notify subscription failed: {last}"));
        }
        let rule = MatchRule::builder()
            .msg_type(MessageType::Signal)
            .interface("org.freedesktop.DBus.Properties")
            .map_err(|e| e.to_string())?
            .member("PropertiesChanged")
            .map_err(|e| e.to_string())?
            .path(path.as_str())
            .map_err(|e| e.to_string())?
            .build();
        let stream = MessageStream::for_match_rule(rule, &conn, Some(32))
            .await
            .map_err(|e| e.to_string())?;
        self.stream = Some(stream);
        sleep(Duration::from_millis(800)).await;
        self.notify_started = true;
        Ok(())
    }

    async fn write_frame(&self, frame: &[u8]) -> Result<(), String> {
        let conn = self.conn()?;
        let path = self.write_path.as_ref().ok_or("no write path")?;
        let chr = Proxy::new(conn, BLUEZ, path.as_str(), CHR_IFACE)
            .await
            .map_err(|e| e.to_string())?;
        let options: HashMap<String, OwnedValue> = HashMap::new();
        timeout(
            Duration::from_secs(5),
            chr.call::<_, _, ()>("WriteValue", &(frame.to_vec(), options)),
        )
        .await
        .map_err(|_| "WriteValue timeout".to_string())?
        .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn take_frame(&mut self) -> Option<Vec<u8>> {
        loop {
            if self.buffer.len() < 6 {
                return None;
            }
            let mut awaiting = false;
            for candidate in 0..=255u8 {
                let head: Vec<u8> = self.buffer[..6].iter().map(|b| b ^ candidate).collect();
                let magic = (head[0], head[1]);
                if magic != MAGIC_V0 && magic != MAGIC_V1 && magic != MAGIC_V2 {
                    continue;
                }
                let len = ((head[2] as usize) << 8) | head[3] as usize;
                if len > 512 {
                    continue;
                }
                if 6 + len > self.buffer.len() {
                    awaiting = true;
                    continue;
                }
                if decode_frame(&self.buffer[..6 + len], &self.mac).is_some() {
                    let frame = self.buffer[..6 + len].to_vec();
                    self.buffer.drain(..6 + len);
                    return Some(frame);
                }
            }
            if awaiting {
                return None;
            }
            if self.buffer.len() > 128 {
                self.buffer.clear();
                return None;
            }
            self.buffer.drain(..1);
        }
    }

    async fn transact(&mut self, frame: &[u8], wait: Duration) -> Result<Vec<u8>, String> {
        self.start_notify().await?;
        self.buffer.clear();
        let mut last_err = String::new();
        if !self.ready {
            self.ready = self.wait_connected(2).await;
        }
        for attempt in 0..4u64 {
            if self.ready {
                match self.write_frame(frame).await {
                    Ok(()) => {
                        last_err.clear();
                        break;
                    }
                    Err(e) => {
                        last_err = e;
                        self.ready = false;
                    }
                }
            } else {
                last_err = "not connected".to_string();
            }
            if !self.ready {
                self.ready = self.wait_connected(2).await;
            }
            sleep(Duration::from_millis(300 * (attempt + 1))).await;
        }
        if !last_err.is_empty() {
            return Err(format!("write failed: {last_err}"));
        }
        let deadline = Instant::now() + wait;
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            let stream = self.stream.as_mut().ok_or("no signal stream")?;
            match timeout(left, stream.next()).await {
                Ok(Some(Ok(msg))) => {
                    if let Ok((_iface, changed, _invalid)) = msg
                        .body()
                        .deserialize::<(String, HashMap<String, OwnedValue>, Vec<String>)>()
                    {
                        if let Some(value) = changed.get("Value") {
                            if let Some(bytes) = bytes_from_value(value) {
                                self.buffer.extend_from_slice(&bytes);
                            }
                        }
                    }
                    if let Some(frame) = self.take_frame() {
                        return Ok(frame);
                    }
                }
                Ok(Some(Err(_))) | Ok(None) => break,
                Err(_) => break,
            }
        }
        Err("device did not reply".to_string())
    }

    async fn read(&mut self) -> Result<Reading, String> {
        let frame = build_frame(&self.mac, &self.payload);
        let reply = self.transact(&frame, Duration::from_secs(5)).await?;
        let payload = decode_frame(&reply, &self.mac).ok_or("bad reply frame")?;
        parse_response(&payload, self.xor).ok_or("bad reply payload".to_string())
    }

    async fn close(&mut self) {
        if let Some(conn) = self.conn.clone() {
            if let Some(path) = self.notify_path.clone() {
                if self.notify_started {
                    if let Ok(chr) = Proxy::new(&conn, BLUEZ, path.as_str(), CHR_IFACE).await {
                        let _ = timeout(Duration::from_secs(5), chr.call::<_, _, ()>("StopNotify", &())).await;
                    }
                }
            }
            if let Some(path) = self.dev_path.clone() {
                if let Ok(dev) = Proxy::new(&conn, BLUEZ, path.as_str(), DEV_IFACE).await {
                    let _ = timeout(Duration::from_secs(5), dev.call::<_, _, ()>("Disconnect", &())).await;
                }
            }
            sleep(Duration::from_millis(300)).await;
        }
        self.notify_started = false;
        self.ready = false;
        self.stream = None;
        self.dev_path = None;
        self.notify_path = None;
        self.write_path = None;
    }

    async fn reset(&mut self) {
        self.close().await;
        self.conn = None;
        self.buffer.clear();
    }
}

fn value_string(v: &OwnedValue) -> Option<String> {
    String::try_from(v.clone()).ok()
}

fn bytes_from_value(v: &OwnedValue) -> Option<Vec<u8>> {
    Vec::<u8>::try_from(v.clone()).ok()
}

// -------------------------------------------------------------------- main

fn parse_args() -> (String, f64, u32, String, u16, f64) {
    let args: Vec<String> = std::env::args().collect();
    let mut config = String::from("/etc/yunmu-meter/meters.toml");
    let mut interval = 0.8f64;
    let mut reads = 2u32;
    let mut host = String::from("0.0.0.0");
    let mut port = 9103u16;
    let mut watchdog = 300f64;
    let mut i = 1;
    while i < args.len() {
        let key = args[i].as_str();
        let mut value = || -> String {
            i += 1;
            args.get(i).cloned().unwrap_or_default()
        };
        match key {
            "--config" => config = value(),
            "--interval" => interval = value().parse().unwrap_or(0.8),
            "--reads-per-cycle" => reads = value().parse().unwrap_or(2),
            "--host" => host = value(),
            "--port" => port = value().parse().unwrap_or(9103),
            "--watchdog-seconds" => watchdog = value().parse().unwrap_or(300.0),
            "--help" | "-h" => {
                println!("yunmu-meter [--config PATH] [--interval S] [--reads-per-cycle N] [--host H] [--port P] [--watchdog-seconds S]");
                exit(0);
            }
            _ => {}
        }
        i += 1;
    }
    (config, interval, reads, host, port, watchdog)
}

fn watchdog(shared: Arc<Mutex<Shared>>, threshold: f64) {
    let started = now_epoch();
    loop {
        std::thread::sleep(Duration::from_secs(30));
        let shared = shared.lock().unwrap();
        let last = shared.devices.iter().map(|d| d.last_ts).fold(0.0, f64::max);
        let now = now_epoch();
        let dead = if last == 0.0 {
            now - started > threshold.max(120.0)
        } else {
            now - last > threshold
        };
        if dead {
            let stale = if last > 0.0 { now - last } else { now - started };
            eprintln!(
                "yunmu-meter: watchdog: no successful read for {stale:.0}s; exiting for systemd restart"
            );
            drop(shared);
            exit(1);
        }
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let (config_path, interval, reads_per_cycle, host, port, watchdog_secs) = parse_args();
    let entries = load_config(&config_path);
    if entries.is_empty() {
        eprintln!("yunmu-meter: no meters configured");
        exit(2);
    }

    let mut shared = Shared {
        devices: entries
            .iter()
            .map(|e| DevState {
                mac: e.mac.clone(),
                name: e.name.clone(),
                values: Values::default(),
                last_ts: 0.0,
                ok: 0.0,
                duration: 0.0,
                error: String::new(),
            })
            .collect(),
    };
    // registry order is the config order; keep the devices list stable
    shared.devices.shrink_to_fit();
    let shared = Arc::new(Mutex::new(shared));

    let listener = match TcpListener::bind((host.as_str(), port)) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("yunmu-meter: cannot bind {host}:{port}: {e}");
            exit(2);
        }
    };
    println!("serving metrics on http://{host}:{port}/metrics ({} meters)", entries.len());
    let http_shared = Arc::clone(&shared);
    std::thread::spawn(move || http_loop(listener, http_shared));

    if watchdog_secs > 0.0 {
        let wd_shared = Arc::clone(&shared);
        std::thread::spawn(move || watchdog(wd_shared, watchdog_secs));
    }

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        tokio::spawn(async move {
            let _ = tokio::signal::ctrl_c().await;
            stop.store(true, Ordering::SeqCst);
        });
    }

    let mut meters: Vec<Meter> = entries
        .iter()
        .enumerate()
        .map(|(i, e)| Meter::new(e, i))
        .collect();

    let offsets: Vec<Duration> = (0..reads_per_cycle.max(1))
        .map(|i| Duration::from_secs_f64(interval * i as f64 / reads_per_cycle.max(1) as f64))
        .collect();
    let mut next_slot = Instant::now();

    'outer: loop {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        for offset in &offsets {
            let due = next_slot + *offset;
            let now = Instant::now();
            if due > now {
                sleep(due - now).await;
            }
            for meter in meters.iter_mut() {
                if stop.load(Ordering::SeqCst) {
                    break 'outer;
                }
                if Instant::now() < meter.skip_until {
                    continue;
                }
                let started = Instant::now();
                let result = async {
                    if meter.offline {
                        meter.connect().await?;
                        meter.offline = false;
                    }
                    meter.read().await
                }
                .await;
                let elapsed = started.elapsed().as_secs_f64();
                let idx = meter.pub_state_index;
                match result {
                    Ok(reading) => {
                        meter.failures = 0;
                        let mut s = shared.lock().unwrap();
                        if let Some(d) = s.devices.get_mut(idx) {
                            d.values = Values {
                                voltage: reading.voltage,
                                current: reading.current,
                                power: reading.power,
                                power_factor: reading.power_factor,
                                threshold: reading.threshold,
                                counter: reading.counter,
                                energy_device: reading.counter as f64 * WH_PER_COUNT / 1000.0,
                            };
                            d.last_ts = now_epoch();
                            d.ok = 1.0;
                            d.duration = elapsed;
                            d.error.clear();
                        }
                    }
                    Err(err) => {
                        meter.offline = true;
                        meter.failures += 1;
                        {
                            let mut s = shared.lock().unwrap();
                            if let Some(d) = s.devices.get_mut(idx) {
                                d.ok = 0.0;
                                d.duration = elapsed;
                                d.error = err.clone();
                            }
                        }
                        meter.close().await;
                        if meter.failures >= 3 {
                            meter.reset().await;
                            meter.failures = 0;
                        }
                        meter.skip_until = Instant::now() + Duration::from_secs(15);
                    }
                }
            }
        }
        next_slot += Duration::from_secs_f64(interval);
        if Instant::now() > next_slot + Duration::from_secs_f64(interval) {
            next_slot = Instant::now();
        }
    }

    for meter in meters.iter_mut() {
        meter.close().await;
    }
}
