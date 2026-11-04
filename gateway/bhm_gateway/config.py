"""Load and validate the gateway TOML config."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConfigError(Exception):
    pass


@dataclass
class BatteryConfig:
    id: str
    port: str
    nominal_voltage: float | None = None
    chemistry: str = "LiFePO4"
    address: int = 0x40
    baudrate: int = 9600
    current_sign: int = 1
    timeout_s: float = 0.6


@dataclass
class MqttConfig:
    host: str
    port: int = 8883
    client_id: str = ""
    topic_prefix: str = "bhm"
    keepalive_s: int = 60
    qos: int = 1
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    username: str = ""
    password_file: str = ""
    password: str = field(default="", repr=False)  # resolved at load time, never in the file


@dataclass
class GatewayConfig:
    site_id: str
    poll_interval_s: float = 10.0
    buffer_path: str = "/var/lib/bhm/buffer.db"
    buffer_max_rows: int = 200_000
    log_level: str = "INFO"


@dataclass
class Config:
    gateway: GatewayConfig
    mqtt: MqttConfig
    batteries: list[BatteryConfig]


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p)) if p else p


def _check_secret_file(path: str, what: str) -> None:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{what} {path} is readable by group/others; chmod 600 it")


def load(path: str | os.PathLike) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"bad TOML in {path}: {e}")

    try:
        gw = GatewayConfig(**raw.get("gateway", {}))
        mq = MqttConfig(**raw.get("mqtt", {}))
        bats = [BatteryConfig(**b) for b in raw.get("battery", [])]
    except TypeError as e:
        raise ConfigError(f"unknown or missing config key: {e}")

    if not _ID_RE.match(gw.site_id):
        raise ConfigError("gateway.site_id must be 1-64 chars of [A-Za-z0-9_-]")
    if not bats:
        raise ConfigError("at least one [[battery]] section is required")
    seen = set()
    for b in bats:
        if not _ID_RE.match(b.id):
            raise ConfigError(f"battery id {b.id!r} must be 1-64 chars of [A-Za-z0-9_-]")
        if b.id in seen:
            raise ConfigError(f"duplicate battery id {b.id!r}")
        seen.add(b.id)
        if b.current_sign not in (1, -1):
            raise ConfigError(f"battery {b.id}: current_sign must be 1 or -1")
        b.port = _expand(b.port)

    # --- MQTT security: TLS is mandatory, there is no plaintext mode ---
    if not mq.host:
        raise ConfigError("mqtt.host is required")
    if mq.qos not in (0, 1, 2):
        raise ConfigError("mqtt.qos must be 0, 1 or 2")
    mq.client_id = mq.client_id or f"bhm-gw-{gw.site_id}"
    mq.ca_file = _expand(mq.ca_file)
    mq.cert_file = _expand(mq.cert_file)
    mq.key_file = _expand(mq.key_file)
    mq.password_file = _expand(mq.password_file)
    if mq.ca_file and not os.path.isfile(mq.ca_file):
        raise ConfigError(f"mqtt.ca_file not found: {mq.ca_file}")
    if bool(mq.cert_file) != bool(mq.key_file):
        raise ConfigError("mqtt.cert_file and mqtt.key_file must be set together")
    if mq.key_file:
        if not os.path.isfile(mq.key_file):
            raise ConfigError(f"mqtt.key_file not found: {mq.key_file}")
        _check_secret_file(mq.key_file, "client key")

    mq.password = os.environ.get("BHM_MQTT_PASSWORD", "")
    if not mq.password and mq.password_file:
        if not os.path.isfile(mq.password_file):
            raise ConfigError(f"mqtt.password_file not found: {mq.password_file}")
        _check_secret_file(mq.password_file, "password file")
        mq.password = Path(mq.password_file).read_text().strip()
    if not mq.cert_file and not (mq.username and mq.password):
        raise ConfigError("configure client authentication: a client certificate "
                          "(cert_file/key_file) and/or username + password")

    gw.buffer_path = _expand(gw.buffer_path)
    return Config(gateway=gw, mqtt=mq, batteries=bats)
