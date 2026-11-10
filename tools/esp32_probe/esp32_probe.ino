// Resource probe for the edge collector workload.
//
// Runs the same building blocks as the collector (BLE scan/central, Wi-Fi
// stack, HTTP server, frame encode/parse at ~40 ops/s) and reports heap and
// per-core idle ratios so a target MCU can be sized.
//
// Serial: 115200. Phases:
//   1. radio off, 5 s  -> idle reference
//   2. radios + workload, 10 s -> idle under load

#include <WiFi.h>
#include <WebServer.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static WebServer server(80);
static BLEScan *scan = nullptr;
static volatile uint32_t idle0 = 0, idle1 = 0;
static volatile uint32_t synth_ops = 0;

static void idle_hook0() { idle0++; }
static void idle_hook1() { idle1++; }

// ---------------------------------------------------------------- protocol

static uint16_t crc16(const uint8_t *d, size_t n, uint16_t init, uint16_t poly) {
  uint16_t v = init;
  for (size_t i = 0; i < n; i++) {
    v ^= d[i];
    for (int b = 0; b < 8; b++) v = (v & 1) ? ((v >> 1) ^ poly) : (v >> 1);
  }
  return v;
}

static void frame_round(const uint8_t mac[6]) {
  uint8_t payload[3] = {0xA4, 0xAC, 0xA4};
  uint16_t init = (uint16_t)(mac[5] + mac[4] + 10);
  uint16_t poly = (uint16_t)(((mac[5] + 11) << 8) | mac[4]);
  uint16_t crc = crc16(payload, 3, init, poly);
  uint8_t frame[9] = {0xEC, 0xED, 0x00, 0x03, payload[0], payload[1], payload[2],
                      (uint8_t)(crc >> 8), (uint8_t)crc};
  uint8_t key = mac[0] + mac[1] + mac[2] + mac[3] + mac[4] + mac[5];
  for (int i = 0; i < 9; i++) frame[i] ^= key;

  // synthetic 32-byte reply: parse + checksum like a real reading
  uint8_t reply[32];
  for (int i = 0; i < 32; i++) reply[i] = (uint8_t)(i * 7 + 0x2C);
  uint8_t plain[32];
  uint8_t x = 0x2C;
  uint8_t sum = 0;
  for (int i = 0; i < 32; i++) {
    plain[i] = reply[i] ^ x;
    sum += plain[i];
  }
  (void)sum;
  synth_ops++;
}

static void synth_task(void *) {
  static const uint8_t mac[6] = {0x26, 0x04, 0x18, 0x01, 0x12, 0xF4};
  while (true) {
    frame_round(mac);
    vTaskDelay(pdMS_TO_TICKS(25));  // 40 rounds/s ~= 4 meters x 10 reads/s
  }
}

// --------------------------------------------------------------------- BLE

class ScanCB : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    String name = dev.getName().c_str();
    if (name.startsWith("YUNM")) {
      Serial.printf("SCAN %s rssi=%d\n", name.c_str(), dev.getRSSI());
    }
  }
};

// ---------------------------------------------------------------- reporting

static void report(const char *tag, uint32_t i0, uint32_t i1, uint32_t ms) {
  Serial.printf("%s idle0=%u idle1=%u over=%ums\n", tag, (unsigned)i0,
                (unsigned)i1, (unsigned)ms);
}

static void heap_report() {
  Serial.printf("HEAP free=%u min=%u internal_free=%u internal_largest=%u\n",
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
  Serial.printf("PSRAM free=%u\n",
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  TaskStatus_t tasks[24];
  UBaseType_t n = uxTaskGetSystemState(tasks, 24, nullptr);
  for (UBaseType_t i = 0; i < n; i++) {
    Serial.printf("TASK %-16s stack_hwm=%u\n", tasks[i].pcTaskName,
                  (unsigned)tasks[i].usStackHighWaterMark);
  }
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  esp_register_freertos_idle_hook_for_cpu(idle_hook0, 0);
  esp_register_freertos_idle_hook_for_cpu(idle_hook1, 1);

  // phase 1: no radios, reference idle rate
  uint32_t t0 = millis();
  uint32_t a0 = idle0, a1 = idle1;
  delay(5000);
  uint32_t b0 = idle0, b1 = idle1;
  report("REF", b0 - a0, b1 - a1, millis() - t0);

  // phase 2: bring up the same stacks as the collector
  WiFi.mode(WIFI_STA);  // allocates Wi-Fi/lwIP, no association
  server.on("/metrics", []() { server.send(200, "text/plain", "probe\n"); });
  server.begin();
  BLEDevice::init("yunmu-probe");
  scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new ScanCB(), false);
  scan->setActiveScan(false);
  scan->setInterval(100);
  scan->setWindow(80);
  scan->start(0, nullptr, false);
  xTaskCreatePinnedToCore(synth_task, "synth", 4096, nullptr, 1, nullptr, 1);

  delay(2000);
  uint32_t c0 = idle0, c1 = idle1;
  uint32_t t1 = millis();
  delay(10000);
  uint32_t d0 = idle0, d1 = idle1;
  report("LOAD", d0 - c0, d1 - c1, millis() - t1);
  Serial.printf("SYNTH ops=%u\n", (unsigned)synth_ops);
  heap_report();
}

void loop() {
  server.handleClient();
  delay(2);
}
