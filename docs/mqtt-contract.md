# MQTT data contract (gateway → server)

The server side (broker + ingester + database on Oracle Cloud) should build on this.

## Transport and security

| | |
|---|---|
| Protocol | MQTT 3.1.1 over **TLS ≥ 1.2**, port **8883**. The gateway has no plaintext mode. |
| Server auth | Gateway verifies the broker certificate **and hostname** against `ca.crt` (or system CAs). |
| Client auth | Client certificate (mutual TLS) **and** username/password, one identity per gateway. |
| Authorisation | Broker ACL: gateway `bhm-gw-<site>` may only **write** `bhm/<site>/#`; the ingester reads `bhm/#`. |
| QoS | 1 (at-least-once). Messages are queued on the gateway (SQLite) until PUBACK, so they survive outages and reboots. Duplicates are possible after a reconnect, so de-duplicate on `(battery_id, boot_id, seq)`. |

## Topics

| Topic | Retained | When | Payload |
|---|---|---|---|
| `bhm/<site>/<battery_id>/telemetry` | no | every `poll_interval_s` (10 s) | full snapshot, see below |
| `bhm/<site>/<battery_id>/alarm` | no | when the set of active BMS faults changes | `active`, `raised`, `cleared`, `fault_code` |
| `bhm/<site>/<battery_id>/status` | yes | BMS starts/stops answering | `{"state":"online"\|"offline","ts","error"?}` |
| `bhm/<site>/gateway/status` | yes | connect / clean shutdown / **last will** on connection loss | `{"state":"online"\|"offline","reason"?,"ts"?}` |
| `bhm/<site>/gateway/health` | no | every 60 s | uptime, `buffered`, `sent`, `dropped`, per-battery state |

`battery_id` values in this lab: `kit12v`, `kit36v`, `kit48v`.

## Telemetry payload (schema 1)

```json
{
  "schema": 1, "site": "lab1", "battery_id": "kit48v",
  "nominal_voltage": 51.2, "chemistry": "LiFePO4",
  "ts": "2026-11-04T08:53:16Z", "boot_id": "473af8ddd1d8", "seq": 1,
  "pack": {
    "voltage_v": 51.0, "current_a": 0.3, "power_w": 15.3, "soc_pct": 46.5,
    "state": "idle|charging|discharging", "remaining_ah": 46.58, "cycles": 65,
    "charge_mos": true, "discharge_mos": true,
    "charger_connected": true, "load_connected": false, "bms_life": 200
  },
  "cells": {
    "count": 16, "voltages_mv": [3187, 3173, "..."],
    "max_mv": 3201, "max_cell": 16, "min_mv": 3173, "min_cell": 2,
    "delta_mv": 28, "balancing": [16]
  },
  "temps": { "sensors_c": [25, 26], "max_c": 26, "min_c": 25 },
  "faults": [], "fault_code": 0
}
```

* `ts` is the UTC time of the reading, set by the gateway clock (NTP must be on).
* `current_a`: positive = charging (flip with `current_sign = -1` per battery if a BMS reports it the other way).
* `balancing`: 1-based numbers of the cells being balanced.
* `faults`: names from the Daly 0x98 bitmap, for example `cell_volt_high_l1`, `soc_low_l2` or `short_circuit_fault` (full list in `gateway/bhm_gateway/daly.py`, `FAULT_BITS`).

## Suggested tables for the database step

* `telemetry(battery_id, ts, voltage_v, current_a, soc_pct, remaining_ah, cycles, max_c, min_c, delta_mv, boot_id, seq)`, with a unique key on `(battery_id, boot_id, seq)`
* `cell_voltage(battery_id, ts, cell_no, mv)` for per-cell trends (imbalance and weak-cell detection)
* `alarm(battery_id, ts, raised[], cleared[], active[])`
