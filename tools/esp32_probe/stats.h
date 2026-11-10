#pragma once
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

struct Snap { char name[17]; uint32_t rt; };

static int take_snapshot(Snap *out, int maxn) {
  TaskStatus_t tasks[24];
  UBaseType_t n = uxTaskGetSystemState(tasks, 24, nullptr);
  if (n > (UBaseType_t)maxn) n = maxn;
  for (UBaseType_t i = 0; i < n; i++) {
    strncpy(out[i].name, tasks[i].pcTaskName, 16);
    out[i].name[16] = 0;
    out[i].rt = tasks[i].ulRunTimeCounter;
  }
  return (int)n;
}

static void print_cpu_delta(Snap *a, int na, Snap *b, int nb, uint32_t window_ms) {
  uint64_t total = 0;
  for (int i = 0; i < nb; i++) {
    for (int j = 0; j < na; j++) {
      if (strcmp(a[j].name, b[i].name) == 0) {
        uint32_t d = b[i].rt - a[j].rt;
        total += d;
        Serial.printf("CPU %-16s %6.2f%%\n", b[i].name,
                      d / (window_ms * 10.0));
        break;
      }
    }
  }
  Serial.printf("CPU TOTAL %.1f%% of %d cores (window %ums)\n",
                total / (window_ms * 1000.0 * 2) * 100.0, 2, window_ms);
}

