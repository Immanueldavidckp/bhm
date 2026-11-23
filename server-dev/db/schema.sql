-- BHM database (MySQL 8.4). Loaded automatically the first time the mysql container starts.
-- All times are UTC. Duplicate MQTT deliveries are dropped by the (battery_id, boot_id, seq) keys.

CREATE TABLE IF NOT EXISTS telemetry (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  site              VARCHAR(64)  NOT NULL,
  battery_id        VARCHAR(64)  NOT NULL,
  ts                DATETIME(3)  NOT NULL,           -- reading time (gateway clock)
  nominal_voltage   DECIMAL(6,1),
  chemistry         VARCHAR(16),
  voltage_v         DECIMAL(7,2),
  current_a         DECIMAL(8,2),                    -- + charging, - discharging
  power_w           DECIMAL(10,1),
  soc_pct           DECIMAL(5,1),
  remaining_ah      DECIMAL(10,3),
  cycles            INT,
  state             VARCHAR(16),                     -- idle | charging | discharging
  charge_mos        BOOLEAN,
  discharge_mos     BOOLEAN,
  charger_connected BOOLEAN,
  load_connected    BOOLEAN,
  cell_count        SMALLINT,
  cell_max_mv       SMALLINT,
  cell_max_no       SMALLINT,
  cell_min_mv       SMALLINT,
  cell_min_no       SMALLINT,
  cell_delta_mv     SMALLINT,
  temp_max_c        SMALLINT,
  temp_min_c        SMALLINT,
  temps_c           JSON,
  faults            JSON,
  fault_code        SMALLINT,
  boot_id           VARCHAR(32)  NOT NULL,
  seq               BIGINT       NOT NULL,
  received_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_telemetry (battery_id, boot_id, seq),
  KEY ix_battery_ts (battery_id, ts)
);

-- one row per cell per reading: per-cell trends, imbalance, weak-cell detection
CREATE TABLE IF NOT EXISTS cell_voltage (
  telemetry_id BIGINT      NOT NULL,
  battery_id   VARCHAR(64) NOT NULL,
  ts           DATETIME(3) NOT NULL,
  cell_no      SMALLINT    NOT NULL,                 -- 1-based
  mv           SMALLINT,
  balancing    BOOLEAN     NOT NULL DEFAULT FALSE,
  PRIMARY KEY (telemetry_id, cell_no),
  KEY ix_cell_trend (battery_id, cell_no, ts),
  CONSTRAINT fk_cell_telemetry FOREIGN KEY (telemetry_id) REFERENCES telemetry (id)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alarm (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  site        VARCHAR(64) NOT NULL,
  battery_id  VARCHAR(64) NOT NULL,
  ts          DATETIME(3) NOT NULL,
  active      JSON,
  raised      JSON,
  cleared     JSON,
  fault_code  SMALLINT,
  boot_id     VARCHAR(32) NOT NULL,
  seq         BIGINT      NOT NULL,
  received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_alarm (battery_id, boot_id, seq),
  KEY ix_alarm_ts (battery_id, ts)
);

-- online/offline changes of each battery and of the gateway itself (incl. MQTT last will)
CREATE TABLE IF NOT EXISTS status_event (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  site        VARCHAR(64) NOT NULL,
  source      VARCHAR(64) NOT NULL,                  -- battery id or 'gateway'
  state       VARCHAR(16) NOT NULL,
  reason      VARCHAR(255),
  ts          DATETIME(3),
  received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY ix_status (source, received_at)
);

CREATE TABLE IF NOT EXISTS gateway_health (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  site        VARCHAR(64) NOT NULL,
  ts          DATETIME(3) NOT NULL,
  boot_id     VARCHAR(32),
  version     VARCHAR(32),
  uptime_s    BIGINT,
  buffered    INT,
  sent        BIGINT,
  dropped     BIGINT,
  batteries   JSON,
  received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY ix_health (site, ts)
);

-- latest reading per battery, for dashboards
CREATE OR REPLACE VIEW battery_latest AS
SELECT t.* FROM telemetry t
JOIN (SELECT battery_id, MAX(id) AS id FROM telemetry GROUP BY battery_id) last
  ON last.id = t.id;
