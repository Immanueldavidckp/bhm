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
| `gateway/bhm_gateway/sim/` | Daly BMS simulator: LiFePO4 pack physics and Daly firmware behaviour |
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
* `probe` for bring-up, and a realistic Daly BMS **simulator** (see below) for working without batteries.

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

### Status screen (the DK's 4.3" LCD) and `status`

The gateway writes its live state to `/run/bhm/status.json` every second. The installer makes
Weston start the BHM screen instead of ST's demo launcher (`/etc/default/demo-launcher` →
`/opt/bhm-gateway/bhm-display.sh`; delete that file to get the demo launcher back). It shows:

* **MQTT**: connected to the broker (and for how long), or offline and why
* **Network**: interface, IP, Wi-Fi name
* **Upload**: messages sent, still queued on the board, time of the last broker ACK
* **one card per battery**: `LIVE` (answering and its data reaching the broker),
  `READING` (answering, data queued because the broker is offline), `NO RESPONSE`
  (port missing / BMS not answering), `WRONG PACK?` (cell count does not match the configured
  nominal voltage, e.g. a 4S pack on the 48 V port), plus SOC, V, A, cells, temperature, faults
* footer: "N of M batteries sharing data with the server"

Same check as text on the console (exit code 0 = all OK):
```sh
PYTHONPATH=/opt/bhm-gateway/lib python3 -m bhm_gateway status
```

## BMS simulator (no batteries needed)

`python3 -m bhm_gateway sim` behaves like the three Daly BMSes on the wire, with the same
frames, timing (9600 baud plus BMS latency) and multi-frame answers. Behind the protocol is a
real LiFePO4 model:

* **Cells:** each cell has its own capacity spread, OCV curve (flat plateau, steep knees),
  internal resistance, polarisation sag and recovery, and self-discharge. Packs are
  top-balanced, so cells agree when full and diverge near empty.
* **Daly firmware:** coulomb-counted SOC against the rated Ah (it drifts from the true SOC and
  snaps to 100 % at end of charge), a cycle counter, and passive balancing (35 mA above
  3.40 V). L1 alarms and L2 protections switch the charge/discharge MOSFETs and release with
  hysteresis, by reverse current, or after a 32 s overcurrent retry.
* **What's connected (`profile`):** `cycle` (discharge, rest, CC/CV charge, rest; the default),
  `drive` (inverter-style random loads, recharge when low), `charge`, `discharge`, `idle`.
* **Thermal:** I²R heating towards ambient, with a day/night swing.

Presets: `12v` = 4S, `24v` = 8S, `36v` = 12S, `48v` = 16S (guessed from names like `kit48v`).
Pack spec: `NAME[:PRESET][@PORT][,key=value...]`, with keys `ah soh soc cycles temps ambient
profile charge_a load_a max_charge_a max_discharge_a`. Run `python3 -m bhm_gateway sim -h`
for the full list.

**A. On the STM32MP135 itself:** the simulator is stdlib only and ships in the bundle.
```sh
systemctl start bhm-sim                      # /run/bhm-sim/kit12v, kit36v, kit48v
# in /etc/bhm/bhm.toml:  port = "/run/bhm-sim/kit48v"   (one per [[battery]])
systemctl restart bhm-gateway
PYTHONPATH=/opt/bhm-gateway/lib python3 -m bhm_gateway simctl status
```
Change packs or speed in `/etc/bhm/sim.env`: `BHM_SIM_ARGS="kit12v kit36v kit48v,profile=drive --speed 10"`.
SOC and cycles persist across restarts (`/var/lib/bhm-sim/state.json`).

**B. On a PC, sent to the MP135:**
```sh
# over the LAN, no extra hardware:   gateway port = "socket://<pc-ip>:7001"
python3 -m bhm_gateway sim kit12v@tcp:7001 kit36v@tcp:7002 kit48v@tcp:7003
# over real RS485, one USB adapter pair per pack (A-A, B-B, GND):
python3 -m bhm_gateway sim kit12v@/dev/ttyUSB0 kit36v@/dev/ttyUSB1 kit48v@/dev/ttyUSB2
```
Use option B over RS485 to test the wiring and adapters the real BMSes will use. Use
option A or TCP to test everything above the serial port.

**D. On a PC, over the board's console UART (ST-LINK USB cable, no network needed):**
the board's only UART to the PC is its console (`ttySTM0` = `/dev/ttyACM0` on the PC), so
`uartlink` multiplexes the three packs' Daly traffic over it in CRC-checked frames. The board
side (`bhm-uartlink.service`) takes the console over from the login shell and gives the gateway
`/run/bhm-uart/kit12v`, `kit36v`, `kit48v`.
```sh
# board, once:  port = "/run/bhm-uart/kit12v" etc. in /etc/bhm/bhm.toml; systemctl disable --now bhm-sim
# PC (close minicom first; --start types `systemctl start bhm-uartlink` into the board's shell):
python3 -m bhm_gateway sim kit12v kit36v kit48v --link-dir /tmp/bms
python3 -m bhm_gateway uartlink host --port /dev/ttyACM0 --sim-dir /tmp/bms --start
```
On **Windows** (`pip install pyserial`; the COM number is the "STMicroelectronics STLink Virtual
COM Port" in Device Manager → Ports). Start the packs in the same order as on the board, since the
board's 1st pack goes to TCP 7001, the 2nd to 7002, and so on:
```bat
cd gateway
python -m bhm_gateway sim kit12v kit36v kit48v
python -m bhm_gateway uartlink host --port COM5 --start        REM second window
python -m bhm_gateway uartlink release --port COM5            REM afterwards: board shell back
```
`simctl`/`simviz` work on the PC as usual. To get the board's shell back: stop `uartlink host`,
then `python3 -m bhm_gateway uartlink release`, or press **Enter 3 times** in minicom; a reboot
always does it too (the service is not enabled at boot). The kernel console log level is
lowered while the link runs so kernel messages do not land in the stream.

**C. On a Windows laptop** (Python 3.11+; `pip install pyserial` for `probe` and COM ports).
Windows has no PTYs or Unix sockets, so the simulator uses TCP instead: packs without a port
are served on **7001, 7002, 7003...**, and the control channel `simctl`/`simviz` use is
**`tcp:127.0.0.1:7000`** (the default on Windows). `--link-dir` is ignored.
```bat
cd gateway
python -m bhm_gateway sim kit12v kit36v kit48v --speed 60
python -m bhm_gateway probe socket://127.0.0.1:7003          REM kit48v
python -m bhm_gateway simctl status
python -m bhm_gateway simviz                                  REM http://127.0.0.1:8765
REM over real RS485 adapters:  python -m bhm_gateway sim kit12v@COM3 kit36v@COM4 kit48v@COM5
```
From the MP135, point each battery at `port = "socket://<laptop-ip>:7001"` (7002, 7003), and
allow Python through Windows Firewall when it asks. Stop the simulator with Ctrl+C.
`--control tcp:HOST:PORT` / `--socket tcp:HOST:PORT` also work on Linux.

**Local test loop:**
```sh
cd gateway && python -m pytest -q
python -m bhm_gateway sim kit12v kit36v kit48v --link-dir /tmp/bms --speed 60   # 1 h per minute
python -m bhm_gateway probe /tmp/bms/kit48v
```

**Inject faults while it runs** (`simctl --socket /tmp/bms/control.sock ...`; on the board the
socket is the default):
```
fault kit48v weak:5:60        cell 5 at 60 % capacity: trips cell_volt_low_l2 on discharge
fault kit48v short            short_circuit_fault, discharge MOS off
fault kit36v open_wire:3      cell 3 reads 0 mV, cell_collect_dropped
fault kit36v ntc_open:2       sensor 2 reads -40 °C, cell_temp_sensor_err
fault kit12v eeprom_err       any Daly fault bit by name
set kit12v ambient=62         real over-temperature trip through the thermal model
set kit48v soc=8 profile=discharge load_a=150    undervoltage, overcurrent
comm kit36v online=0          BMS stops answering (gateway marks it offline)
comm kit36v drop=0.2 corrupt=0.05    flaky RS485 line
clear all                     speed 600       status
```
**Live 3D view** of a running simulator, in the browser:
```sh
python -m bhm_gateway simviz --socket /tmp/bms/control.sock      # open http://127.0.0.1:8765
```
Each pack is drawn as its real prismatic cells, busbars, BMS, charger or load, and the RS485 cables to
the MP135. Cell colour shows voltage and fill height shows that cell's true SOC (so weak or leaky cells
stand out). Dots run along the current path (green for charge, orange for discharge, faster at higher
current), balancing cells pulse, open sense wires and NTCs blink, and the BMS LEDs follow the MOS state.
RS485 requests and replies travel the cables whenever something (the gateway or `probe`) reads that BMS,
and the cable turns red when the BMS is offline. **Charge / Off / Discharge** switches (in the side panel, and as the
green CHG and orange DSG buttons on each charger box; press the lit one again for off) connect the charger or
the load, and two sliders set the charging and discharge current, with the BMS overcurrent alarm shown so you
can trip it on purpose. The side panel also shows the selected pack, a history of BMS vs true SOC, and buttons
for the auto profiles and the same fault and comm commands as `simctl`; click a cell to aim the cell faults
at it. The page loads three.js from a CDN; the simulator needs no internet. `--bind 0.0.0.0`
opens it to the LAN.

**Charge test with simulated data** (a 4-hour bench charge, published like the gateway):
```sh
python3 -m bhm_gateway --config laptop.toml chargetest        # [mqtt] + one [[battery]] like bhm.toml
python3 -m bhm_gateway chargetest --dry-run --out msgs.jsonl --truth truth.json
```
12 V 4S 100 Ah LiFePO4 at 24 °C, starting at 10 % SOC: 10 min rest, 25 A CC/CV charger (14.4 V),
a 2 min charger pause at 50 % SOC, charger stops at 5 A, 15 min rest (about 4 h). Cell 3 is aged
(92 % capacity) and cell 2 has 1.6x the internal resistance. It runs in the simulator in about a
second, is read every 10 s of simulated time with the real Daly decoders and goes through the real
gateway code (seq, online/offline, alarms, buffer), with timestamps over the hours before `--end`
(default now): about 1450 readings, 870 KB, battery id `chargetest12v` ("Charge test 12V" on the
dashboard). Pack options for any simulated pack: `weak=CELL:PCT`, `hir=CELL:FACTOR`,
`profile=chargetest`; runtime fault `hir:<cell>[:<factor>]`.

Protection thresholds are typical Daly LiFePO4 defaults and live in
`gateway/bhm_gateway/sim/firmware.py`. Set them to the values your BMSes are configured with.

## Server on the laptop (for now)

`server-dev/` runs the whole server side in Docker: Mosquitto (TLS, port 8883), MySQL 8.4,
the **ingester** (MQTT `bhm/#` → MySQL, acks only after commit, de-duplicates) and Adminer.
```sh
cd server-dev
./setup_laptop.sh <laptop-ip>        # certs for that IP, MQTT users, DB passwords in .env (gitignored)
docker compose up -d --build
docker compose logs -f ingester
```
* Board side: `mqtt.host = "<laptop-ip>"`, copy `certs/ca.crt`, `certs/bhm-gw-lab1.crt/.key` and
  `secrets/bhm-gw-lab1.password` to `/etc/bhm/` (see bring-up step 4).
* Browse the data at http://127.0.0.1:8080 (Adminer, server `mysql`, user `bhm`, password
  `MYSQL_PASSWORD` in `server-dev/.env`). MySQL and Adminer listen on localhost only.
* Tables: `telemetry` (one row per reading), `cell_voltage` (one row per cell per reading),
  `alarm`, `status_event`, `gateway_health`, view `battery_latest`. Schema: `server-dev/db/schema.sql`.
* If the laptop's IP changes, re-run `./setup_laptop.sh <new-ip>`, `docker compose restart mosquitto`
  and update `mqtt.host` on the board.
* **Web dashboard** (service `dashboard`, http://127.0.0.1:8090, user `bhm`, `DASH_PASSWORD` in
  `.env`): `dashboard.html` reads live telemetry in the browser over MQTT-over-WebSocket.
  `/` redirects to `/?mode=mqtt&url=ws(s)://<host>/mqtt`; `/mqtt` is proxied by `api/api.py` to
  Mosquitto's WebSocket listener 9001 (inside the compose network only, anonymous but read-only:
  `mosquitto/acl-ws`), after the dashboard login (basic auth, or the cookie the page sets).
  `/?mode=sim` shows the page's built-in demo data. In live mode the page first loads the last
  240 stored readings per kit from `/api/recent` (MySQL, same JSON as the gateway's MQTT messages),
  so Overview / Health / Alerts have data while the board is offline; old data is marked "Offline, last data … ago". After editing `dashboard.html`:
  `docker compose up -d --build dashboard`. Old JSON API (`/api/summary|history|events`) still works.
* **Battery and cell health** (`api/health.py`, `/api/health`), calculated from the stored readings
  because the Daly reports neither: **internal resistance of every cell** at each current step
  of 10 A or more (change in cell voltage ÷ change in current, a 10-second DC IR); **capacity of
  every cell** from a charge that runs until the charger finishes, started below ~30 % SOC after
  5 min rest (Ah in ÷ (1 − start SOC from the rested cell voltage)); **pack SOH** = weakest cell ÷
  rated capacity. Each cell is Good / Watch / Replace against the other cells (IR 1.25x / 1.5x,
  capacity 95 % / 85 %). Shown in **Health Monitoring**; **Cells & Frames** steps through every
  reading (`/api/frames`) with the balancing cells and a cell map of all frames.
* **Charge Report** tab: the whole stored window of one kit as a report — a summary in words (SoC,
  Ah / kWh in, CC / CV / pause times, voltage, temperature, largest imbalance, balancing per cell,
  BMS alarms with on/off times, SOH and abnormal cells), the charge phases, 4-hour charts of the
  pack and of every cell (and each cell minus the pack average), a cell-by-cell table, then **every
  frame, 100 per page** (lowest / highest cell coloured, balancing cells green), CSV download of all
  frames, and print / save as PDF.
* Mosquitto `max_queued_messages 100000`: with the default 1000 the broker dropped part of a
  backlog burst on its way to the ingester.

## Next: server side (Oracle Cloud)

Run Mosquitto with `server-dev/mosquitto/*` and a **real hostname** in the server certificate
(`./gen_certs.sh mqtt.<your-domain> bhm-gw-lab1 bhm-ingest`), open only TCP 8883 in the VCN security
list, then add an ingester that subscribes as `bhm-ingest` and writes to the database following
`docs/mqtt-contract.md`.
