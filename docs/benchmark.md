# Python vs Rust resource use

Measured on the production Raspberry Pi 4 (aarch64, Debian 13, BlueZ 5.82) with
the same workload: 4 meters, `--interval 0.8`, 2 reads per cycle (10 reads/s).

Method: 60 s of `/proc/<pid>/stat` utime+stime delta for CPU, `VmRSS` from
`/proc/<pid>/status`. Disk is the runtime + required shared libraries, not the
source tree.

| Metric | Python service | Rust service | Change |
|---|---|---|---|
| Resident memory (VmRSS) | 28.5 MB | **2.96 MB** | −89.6 % (9.6×) |
| CPU, 10 reads/s | 10.4 % of one core | **1.53 %** | −85.3 % (6.8×) |
| Threads | 4 | 3 | |
| Binary / runtime stack on disk | ~67 MB¹ | **1.2 MB** | ~55× |
| Runtime dependencies | Python 3.13 stdlib (53 MB), libpython (7.2 MB), PyGObject, dbus-python, GLib, libdbus | libc, libgcc | |

¹ `/usr/lib/python3.13` 53 MB + `libpython3.13.so` 7.2 MB + `python3-gi` 0.8 MB +
`python3-dbus` 0.5 MB + `libglib2.0` 5 MB + `libdbus` 0.5 MB.

Notes:

- The Python figure is the fixed service (signal-handler leak removed, watchdog
  added), measured after a restart; it is the fair baseline for the same work.
- The Rust figure is after caching the BLE link readiness state, which removed
  two D-Bus property round trips per read: CPU fell from 2.13 % to 1.53 % with no
  change in memory.
- Rust release profile: `opt-level = "z"`, LTO, `codegen-units = 1`,
  `panic = "abort"`, stripped.
- Both services expose the same series; values matched during the swap.

Raw files: not committed (measurements are environmental). Re-run with
`tools/measure_resources.sh PID 60`.
