# BHM: Battery Health Monitoring

Three LiFePO4 training kits (12 V, 36 V, 48 V), each with a **Daly BMS**, are read over
**RS485 → USB** by an **STM32MP135** (OpenSTLinux). Readings are published **securely over
MQTT/TLS** to a server on Oracle Cloud.

```
Daly BMS 12V ─RS485─[USB adapter]─┐
Daly BMS 36V ─RS485─[USB adapter]─┼─ STM32MP135 ── bhm-gateway ══ MQTT/TLS 8883 (mTLS + password) ══▶ broker ─▶ ingester ─▶ DB
Daly BMS 48V ─RS485─[USB adapter]─┘     (SQLite store-and-forward buffer)
```

| Path | What |
|---|---|
| `gateway/` | The gateway service that runs on the STM32MP135 (Python 3.11+, pure Python) |
| `gateway/deploy/` | systemd unit, board installer, offline bundle builder |
| `docs/mqtt-contract.md` | Topics, payloads and security model, the interface for the server side |
| `server-dev/` | Local TLS Mosquitto (Docker), CA and certificate scripts, ACL, also a starting point for the cloud broker |

## Gateway features

* Daly RS485/UART protocol: pack voltage, current, SOC, remaining Ah, cycles, MOS state, **every
  cell voltage**, every temperature sensor, balancing cells, and the decoded **fault bitmap**.
* **TLS only** (≥ 1.2, server cert and hostname verified), client certificate plus password, and secrets
  passed through systemd credentials. The service runs as an unprivileged sandboxed user.
* **No data loss on outages**: every reading goes to an SQLite queue first and is removed only after the
  broker's QoS 1 ack. The queue is capped at about 23 days of data.
* Retained online/offline status per battery and for the gateway (MQTT last will), alarm events on
  fault changes, and a health heartbeat.
* `probe` and `sim` tools for bring-up without needing the server, or even the hardware.

## Bring-up on the board

1. **Build the bundle** on a PC (it vendors pyserial and paho-mqtt, so the board needs no internet):
   ```sh
   cd gateway && deploy/make_bundle.sh        # -> dist/bhm-gateway-<ver>.tar.gz
   scp dist/bhm-gateway-*.tar.gz root@<board>:/tmp/
   ```
2. **Install** on the board:
   ```sh
   cd /tmp && tar xzf bhm-gateway-*.tar.gz && cd bhm-gateway && ./install.sh
   ```
3. **Find each BMS.** Plug in one USB-RS485 adapter at a time (A→A, B→B, GND), then:
   ```sh
   ls -l /dev/serial/by-path/        # stable name per USB socket
   PYTHONPATH=/opt/bhm-gateway/lib python3 -m bhm_gateway probe /dev/ttyUSB0
   ```
   `probe` tries request address `0x40` (RS485) then `0x80` (UART) and prints the decoded snapshot.
   Check that `current_a` is positive while charging; if not, set `current_sign = -1`.
4. **Configure** `/etc/bhm/bhm.toml` (broker host, `site_id`, one `[[battery]]` per BMS using the
   `/dev/serial/by-path/...` names) and copy the credentials:
   ```
   /etc/bhm/certs/ca.crt        CA that signed the broker certificate
   /etc/bhm/certs/client.crt    this gateway's certificate
   /etc/bhm/certs/client.key    its private key        (chmod 600)
   /etc/bhm/mqtt_password       MQTT password, one line  (chmod 600)
   ```
5. `systemctl restart bhm-gateway && journalctl -u bhm-gateway -f`

> **Clock:** TLS certificate checks fail if the board's clock is wrong. Keep NTP enabled
> (`timedatectl set-ntp true`, which the installer does) and make sure the board has network before the service starts.

> **One adapter per BMS:** stock Daly firmware answers every request on the bus, so the three
> BMSes cannot share one RS485 line. Use three adapters, or a USB hub, on the MP135.

## Test without hardware

```sh
cd gateway && python -m pytest -q                    # protocol, buffer and simulator tests
python -m bhm_gateway sim kit12v:4 kit36v:12 kit48v:16 --link-dir /tmp/bms   # fake BMSes on PTYs
python -m bhm_gateway probe /tmp/bms/kit48v
```

Full end-to-end run against a local TLS broker: see `server-dev/docker-compose.yml`
(`./gen_certs.sh localhost bhm-gw-lab1 bhm-ingest`, `./add_user.sh ...`, `docker compose up -d`).

## Next: server side (Oracle Cloud)

Run Mosquitto with `server-dev/mosquitto/*` and a **real hostname** in the server certificate
(`./gen_certs.sh mqtt.<your-domain> bhm-gw-lab1 bhm-ingest`), open only TCP 8883 in the VCN security
list, then add an ingester that subscribes as `bhm-ingest` and writes to the database following
`docs/mqtt-contract.md`.
