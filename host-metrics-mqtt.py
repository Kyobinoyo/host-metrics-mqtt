#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
host-metrics-mqtt - publish Linux host metrics to Home Assistant via MQTT.

Entities are created automatically through Home Assistant MQTT discovery and
grouped under one device per host. Everything is configured in a separate
YAML file, see config.example.yaml.

Requirements: Python >= 3.7, psutil, PyYAML, paho-mqtt (1.x or 2.x)
    Debian/Raspberry Pi OS: sudo apt install python3-psutil python3-yaml python3-paho-mqtt
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import platform
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time

__version__ = "0.3.0"
PROJECT_URL = "https://github.com/Kyobinoyo/host-metrics-mqtt"

log = logging.getLogger("host-metrics-mqtt")

try:
    import psutil
except ImportError:  # pragma: no cover
    sys.exit("psutil is missing - install it: sudo apt install python3-psutil")


# --------------------------------------------------------------------------
# Entity names (en / de). "{}" is replaced by disk, device or interface.
# --------------------------------------------------------------------------
TEXT = {
    "cpu_usage": ("CPU usage", "CPU-Auslastung"),
    "cpu_temperature": ("CPU temperature", "CPU-Temperatur"),
    "cpu_frequency": ("CPU frequency", "CPU-Takt"),
    "load_1": ("Load 1 min", "Last 1 min"),
    "load_5": ("Load 5 min", "Last 5 min"),
    "load_15": ("Load 15 min", "Last 15 min"),
    "memory_usage": ("Memory usage", "RAM-Auslastung"),
    "memory_used": ("Memory used", "RAM belegt"),
    "memory_available": ("Memory available", "RAM verfügbar"),
    "swap_usage": ("Swap usage", "Swap-Auslastung"),
    "processes": ("Processes", "Prozesse"),
    "last_boot": ("Last boot", "Letzter Start"),
    "kernel": ("Kernel", "Kernel"),
    "os": ("Operating system", "Betriebssystem"),
    "disk_usage": ("{} usage", "{} Belegung"),
    "disk_used": ("{} used", "{} belegt"),
    "disk_free": ("{} free", "{} frei"),
    "disk_read_only": ("{} read-only", "{} schreibgeschützt"),
    "fs_errors": ("{} filesystem errors", "{} Dateisystemfehler"),
    "io_read_rate": ("{} read rate", "{} Leserate"),
    "io_write_rate": ("{} write rate", "{} Schreibrate"),
    "io_written": ("{} written since boot", "{} geschrieben seit Start"),
    "io_life_time": ("{} wear level", "{} Verschleiß"),
    "io_pre_eol": ("{} pre-EOL state", "{} Vor-EOL-Status"),
    "net_rx_rate": ("{} download", "{} Download"),
    "net_tx_rate": ("{} upload", "{} Upload"),
    "net_rx_total": ("{} received since boot", "{} empfangen seit Start"),
    "net_tx_total": ("{} sent since boot", "{} gesendet seit Start"),
    "net_up": ("{} link", "{} Verbindung"),
    "net_speed": ("{} link speed", "{} Verbindungsgeschwindigkeit"),
    "net_errors": ("{} errors", "{} Fehler"),
    "net_dropped": ("{} dropped packets", "{} verworfene Pakete"),
    "pi_undervoltage": ("Under-voltage", "Unterspannung"),
    "pi_throttled": ("Throttled", "Gedrosselt"),
    "pi_freq_capped": ("Frequency capped", "Takt begrenzt"),
    "pi_undervoltage_occurred": ("Under-voltage since boot", "Unterspannung seit Start"),
    "pi_throttled_occurred": ("Throttled since boot", "Gedrosselt seit Start"),
    "pi_throttle_flags": ("Throttle flags", "Drossel-Flags"),
    "updates": ("Pending updates", "Ausstehende Updates"),
    # values of yes/no sensors
    "yes": ("Yes", "Ja"),
    "no": ("No", "Nein"),
    # default names for disks and devices without a configured label
    "label_root": ("Root filesystem", "Systempartition"),
    "label_boot": ("Boot partition", "Bootpartition"),
    "label_sd": ("SD card", "SD-Karte"),
}

LABEL_KEYS = ("disks", "network", "block_devices")

# Entities of older versions that no longer exist. Their retained discovery
# messages are cleared on every discovery so Home Assistant drops them.
REMOVED_ENTITIES = (
    ("sensor", "reboot_required"),
    ("binary_sensor", "reboot_required"),
)

DIAG = "diagnostic"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "root"


def read_text(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def rnd(value, digits=1):
    return None if value is None else round(value, digits)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEFAULTS = {
    "mqtt": {
        "host": "localhost",
        "port": "1883",
        "username": "",
        "password": "",
        "password_file": "",
        "client_id": "",
        "keepalive": "60",
        "tls": "no",
        "tls_ca": "",
        "tls_insecure": "no",
    },
    "homeassistant": {
        "discovery_prefix": "homeassistant",
        "status_topic": "homeassistant/status",
    },
    "device": {
        "node_id": "",
        "name": "",
        "base_topic": "host-metrics",
    },
    "general": {
        "interval": "30",
        "expire_after": "auto",
        "language": "en",
        "flags": "text",
        "log_level": "INFO",
    },
    "metrics": {
        "cpu": "yes",
        "temperature": "yes",
        "temperature_sensor": "auto",
        "frequency": "yes",
        "load": "yes",
        "memory": "yes",
        "swap": "yes",
        "processes": "yes",
        "uptime": "yes",
        "system_info": "yes",
        "disks": "/",
        "network": "auto",
        "block_devices": "auto",
        "raspberry_pi": "auto",
        "updates": "yes",
        "updates_interval": "3600",
    },
}


class ConfigError(Exception):
    pass


def find_config(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    candidates = []
    if cred:
        candidates.append(os.path.join(cred, "config"))
    candidates += ["/etc/host-metrics-mqtt/config.yaml", "./config.yaml"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _to_str(value) -> str:
    """YAML value -> the string form the rest of the script works with."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return " ".join(_to_str(v) for v in value)
    if isinstance(value, dict):
        raise ConfigError("unexpected nested mapping: %r" % (value,))
    return str(value)


def load_config(path: str | None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(DEFAULTS)
    cfg.labels = {}
    if not path:
        return cfg
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is missing - install it: sudo apt install python3-yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s: top level must be a mapping" % path)
    labels = {}
    cfg.labels = labels
    for section, values in data.items():
        if section not in DEFAULTS:
            raise ConfigError("unknown section '%s' (known: %s)"
                              % (section, ", ".join(DEFAULTS)))
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError("section '%s' must be a mapping" % section)
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                raise ConfigError("unknown option '%s.%s'" % (section, key))
            if section == "metrics" and key in LABEL_KEYS and isinstance(value, dict):
                # mapping form: {name: label}
                labels[key] = {str(k): _to_str(v) for k, v in value.items()}
                value = list(labels[key])
            cfg[section][key] = _to_str(value)
    if cfg["general"]["flags"].strip().lower() not in ("text", "binary"):
        raise ConfigError("general.flags must be text or binary")
    # type check early instead of failing somewhere in the main loop
    for section, key in (("mqtt", "port"), ("mqtt", "keepalive"), ("general", "interval"),
                         ("metrics", "updates_interval")):
        try:
            cfg[section].getint(key)
        except ValueError:
            raise ConfigError("%s.%s must be a number" % (section, key))
    for section, keys in DEFAULTS.items():
        for key, default in keys.items():
            if default in ("yes", "no"):
                try:
                    cfg[section].getboolean(key)
                except ValueError:
                    raise ConfigError("%s.%s must be true or false" % (section, key))
    return cfg


def as_list(value: str) -> list:
    return [v for v in re.split(r"[\s,]+", value.strip()) if v]


def auto_bool(value: str, detected: bool) -> bool:
    v = value.strip().lower()
    if v == "auto":
        return detected
    return v in ("1", "yes", "true", "on")


# --------------------------------------------------------------------------
# Entity model
# --------------------------------------------------------------------------
class Entity:
    def __init__(self, key, text_key, component="sensor", arg=None, unit=None,
                 device_class=None, state_class=None, icon=None, category=None,
                 precision=None, expire=True, kind=None, flag=False):
        self.key = key
        self.text_key = text_key
        self.arg = arg
        self.component = component
        self.unit = unit
        self.device_class = device_class
        self.state_class = state_class
        self.icon = icon
        self.category = category
        self.precision = precision
        self.expire = expire
        self.kind = kind            # metrics key the arg belongs to (for labels)
        self.flag = flag            # yes/no value, shown as text or as binary sensor
        self.legacy_component = None


def binary_template(field: str) -> str:
    return ("{% if value_json." + field + " is none %}None"
            "{% elif value_json." + field + " %}ON{% else %}OFF{% endif %}")


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------
class Host:
    """Static information and collectors for the local machine."""

    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg
        m = cfg["metrics"]
        self.model = (read_text("/proc/device-tree/model") or "").replace("\x00", "")
        self.is_pi = "raspberry pi" in self.model.lower()
        if not self.model:
            vendor = read_text("/sys/class/dmi/id/sys_vendor") or ""
            product = read_text("/sys/class/dmi/id/product_name") or ""
            self.model = " ".join(p for p in (vendor, product) if p) or platform.machine()
        self.os_name = self._os_name()

        self.disks = as_list(m["disks"])
        self.nics = self._resolve_nics(m["network"])
        self.block_devices = self._resolve_block_devices(m["block_devices"])
        self.pi_enabled = auto_bool(m["raspberry_pi"], self.is_pi)
        self.temp_sensor = m["temperature_sensor"].strip()

        self._last_net = None
        self._last_io = None
        self._updates_cache = (None, 0.0)
        # prime counters so the first published cycle already has rates
        psutil.cpu_percent(interval=None)
        self._block_io()
        self._network()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _os_name():
        data = {}
        for line in (read_text("/etc/os-release") or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k] = v.strip('"')
        return data.get("PRETTY_NAME") or platform.system()

    @staticmethod
    def _resolve_nics(value):
        if value.strip().lower() != "auto":
            return as_list(value)
        skip = ("lo", "docker", "veth", "br-", "virbr", "vnet", "cali", "flannel", "cni",
                "ifb", "dummy", "sit", "ip6tnl", "tunl", "gre", "erspan")
        return sorted(n for n in psutil.net_if_addrs() if not n.startswith(skip))

    @staticmethod
    def _resolve_block_devices(value):
        v = value.strip().lower()
        if v in ("", "no", "none", "off"):
            return []
        if v != "auto":
            return as_list(value)
        skip = ("loop", "ram", "zram", "sr", "fd", "nbd")
        devs = []
        for name in sorted(os.listdir("/sys/block")):
            if name.startswith(skip) or re.match(r"mmcblk\d+boot\d+$", name):
                continue
            size = read_text("/sys/block/%s/size" % name)
            if size and size.isdigit() and int(size) > 0:
                devs.append(name)
        return devs

    @staticmethod
    def partition_of(path):
        """Kernel name of the block device a path lives on (e.g. mmcblk0p2)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        link = "/sys/dev/block/%d:%d" % (os.major(st.st_dev), os.minor(st.st_dev))
        if not os.path.exists(link):
            return None
        return os.path.basename(os.path.realpath(link))

    # -- entities ----------------------------------------------------------
    def entities(self):
        m = self.cfg["metrics"]
        on = lambda k: m.getboolean(k)  # noqa: E731
        e = []
        if on("cpu"):
            e.append(Entity("cpu_usage", "cpu_usage", unit="%", state_class="measurement",
                            icon="mdi:cpu-64-bit", precision=1))
        if on("temperature"):
            e.append(Entity("cpu_temperature", "cpu_temperature", unit="°C",
                            device_class="temperature", state_class="measurement", precision=1))
        if on("frequency"):
            e.append(Entity("cpu_frequency", "cpu_frequency", unit="MHz", device_class="frequency",
                            state_class="measurement", precision=0))
        if on("load"):
            for k in ("load_1", "load_5", "load_15"):
                e.append(Entity(k, k, state_class="measurement", icon="mdi:gauge", precision=2))
        if on("memory"):
            e.append(Entity("memory_usage", "memory_usage", unit="%", state_class="measurement",
                            icon="mdi:memory", precision=1))
            e.append(Entity("memory_used", "memory_used", unit="MiB", device_class="data_size",
                            state_class="measurement", icon="mdi:memory", precision=0))
            e.append(Entity("memory_available", "memory_available", unit="MiB",
                            device_class="data_size", state_class="measurement",
                            icon="mdi:memory", precision=0))
        if on("swap"):
            e.append(Entity("swap_usage", "swap_usage", unit="%", state_class="measurement",
                            icon="mdi:swap-horizontal", precision=1))
        if on("processes"):
            e.append(Entity("processes", "processes", state_class="measurement",
                            icon="mdi:application-cog", category=DIAG))
        if on("uptime"):
            e.append(Entity("last_boot", "last_boot", device_class="timestamp",
                            category=DIAG, expire=False))
        if on("system_info"):
            e.append(Entity("kernel", "kernel", icon="mdi:penguin", category=DIAG, expire=False))
            e.append(Entity("os", "os", icon="mdi:linux", category=DIAG, expire=False))

        for path in self.disks:
            s = slug(path)
            e.append(Entity("disk_%s_usage" % s, "disk_usage", arg=path, kind="disks", unit="%",
                            state_class="measurement", icon="mdi:harddisk", precision=1))
            e.append(Entity("disk_%s_used" % s, "disk_used", arg=path, kind="disks", unit="GiB",
                            device_class="data_size", state_class="measurement", precision=2))
            e.append(Entity("disk_%s_free" % s, "disk_free", arg=path, kind="disks", unit="GiB",
                            device_class="data_size", state_class="measurement", precision=2))
            e.append(Entity("disk_%s_read_only" % s, "disk_read_only", component="binary_sensor",
                            arg=path, kind="disks", device_class="problem", icon="mdi:lock-alert", flag=True))
            e.append(Entity("disk_%s_fs_errors" % s, "fs_errors", arg=path, kind="disks",
                            state_class="measurement", icon="mdi:alert-circle-outline",
                            category=DIAG))

        for dev in self.block_devices:
            s = slug(dev)
            e.append(Entity("io_%s_read_rate" % s, "io_read_rate", arg=dev, kind="block_devices", unit="kB/s",
                            device_class="data_rate", state_class="measurement", precision=1))
            e.append(Entity("io_%s_write_rate" % s, "io_write_rate", arg=dev, kind="block_devices", unit="kB/s",
                            device_class="data_rate", state_class="measurement", precision=1))
            e.append(Entity("io_%s_written" % s, "io_written", arg=dev, kind="block_devices", unit="GiB",
                            device_class="data_size", state_class="total_increasing", precision=2))
            if os.path.exists("/sys/block/%s/device/life_time" % dev):
                e.append(Entity("io_%s_life_time" % s, "io_life_time", arg=dev, kind="block_devices", unit="%",
                                state_class="measurement", icon="mdi:sd", precision=0))
            if os.path.exists("/sys/block/%s/device/pre_eol_info" % dev):
                e.append(Entity("io_%s_pre_eol" % s, "io_pre_eol", arg=dev, kind="block_devices", icon="mdi:sd"))

        for nic in self.nics:
            s = slug(nic)
            e.append(Entity("net_%s_rx_rate" % s, "net_rx_rate", arg=nic, kind="network", unit="kbit/s",
                            device_class="data_rate", state_class="measurement",
                            icon="mdi:download-network", precision=1))
            e.append(Entity("net_%s_tx_rate" % s, "net_tx_rate", arg=nic, kind="network", unit="kbit/s",
                            device_class="data_rate", state_class="measurement",
                            icon="mdi:upload-network", precision=1))
            e.append(Entity("net_%s_rx_total" % s, "net_rx_total", arg=nic, kind="network", unit="GiB",
                            device_class="data_size", state_class="total_increasing", precision=2))
            e.append(Entity("net_%s_tx_total" % s, "net_tx_total", arg=nic, kind="network", unit="GiB",
                            device_class="data_size", state_class="total_increasing", precision=2))
            e.append(Entity("net_%s_up" % s, "net_up", component="binary_sensor", arg=nic, kind="network",
                            device_class="connectivity"))
            e.append(Entity("net_%s_speed" % s, "net_speed", arg=nic, kind="network", unit="Mbit/s",
                            device_class="data_rate", category=DIAG))
            e.append(Entity("net_%s_errors" % s, "net_errors", arg=nic, kind="network", state_class="total_increasing",
                            icon="mdi:alert-circle-outline", category=DIAG))
            e.append(Entity("net_%s_dropped" % s, "net_dropped", arg=nic, kind="network",
                            state_class="total_increasing", icon="mdi:package-variant-remove",
                            category=DIAG))

        if self.pi_enabled:
            icons = {"pi_undervoltage": "mdi:flash-alert", "pi_undervoltage_occurred": "mdi:flash-alert",
                     "pi_throttled": "mdi:speedometer-slow", "pi_throttled_occurred": "mdi:speedometer-slow",
                     "pi_freq_capped": "mdi:speedometer-medium"}
            for k in ("pi_undervoltage", "pi_throttled", "pi_freq_capped",
                      "pi_undervoltage_occurred", "pi_throttled_occurred"):
                e.append(Entity(k, k, component="binary_sensor", device_class="problem",
                                icon=icons[k], flag=True))
            e.append(Entity("pi_throttle_flags", "pi_throttle_flags", icon="mdi:raspberry-pi",
                            category=DIAG))

        if on("updates"):
            e.append(Entity("updates", "updates", icon="mdi:package-up", state_class="measurement",
                            expire=False))
        return e

    # -- collection --------------------------------------------------------
    def collect(self):
        m = self.cfg["metrics"]
        on = lambda k: m.getboolean(k)  # noqa: E731
        d = {}
        if on("cpu"):
            d["cpu_usage"] = rnd(psutil.cpu_percent(interval=None))
        if on("temperature"):
            d["cpu_temperature"] = rnd(self._temperature())
        if on("frequency"):
            try:
                f = psutil.cpu_freq()
                d["cpu_frequency"] = rnd(f.current, 0) if f else None
            except Exception:  # noqa: BLE001
                d["cpu_frequency"] = None
        if on("load"):
            l1, l5, l15 = os.getloadavg()
            d.update(load_1=round(l1, 2), load_5=round(l5, 2), load_15=round(l15, 2))
        if on("memory"):
            vm = psutil.virtual_memory()
            d["memory_usage"] = rnd(vm.percent)
            d["memory_used"] = rnd((vm.total - vm.available) / 2**20, 0)
            d["memory_available"] = rnd(vm.available / 2**20, 0)
        if on("swap"):
            sw = psutil.swap_memory()
            d["swap_usage"] = rnd(sw.percent) if sw.total else 0.0
        if on("processes"):
            d["processes"] = len(psutil.pids())
        if on("uptime"):
            d["last_boot"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                           time.gmtime(int(psutil.boot_time())))
        if on("system_info"):
            d["kernel"] = platform.release()
            d["os"] = self.os_name

        for path in self.disks:
            d.update(self._disk(path))
        d.update(self._block_io())
        d.update(self._network())
        if self.pi_enabled:
            d.update(self._throttled())
        if on("updates"):
            d["updates"] = self._updates(m.getint("updates_interval"))
        return d

    def _temperature(self):
        try:
            temps = psutil.sensors_temperatures()
        except Exception:  # noqa: BLE001
            temps = {}
        if self.temp_sensor.lower() != "auto":
            vals = temps.get(self.temp_sensor)
            return vals[0].current if vals else None
        for name in ("cpu_thermal", "coretemp", "k10temp", "zenpower", "soc_thermal",
                     "cpu-thermal", "acpitz"):
            vals = temps.get(name)
            if vals:
                pkg = [t for t in vals if t.label.lower().startswith(("package", "tctl"))]
                return (pkg or vals)[0].current
        for vals in temps.values():
            if vals:
                return vals[0].current
        raw = read_text("/sys/class/thermal/thermal_zone0/temp")
        return int(raw) / 1000.0 if raw and raw.lstrip("-").isdigit() else None

    def _disk(self, path):
        s = slug(path)
        out = {"disk_%s_usage" % s: None, "disk_%s_used" % s: None,
               "disk_%s_free" % s: None, "disk_%s_read_only" % s: None,
               "disk_%s_fs_errors" % s: None}
        try:
            u = psutil.disk_usage(path)
            out["disk_%s_usage" % s] = rnd(u.percent)
            out["disk_%s_used" % s] = rnd(u.used / 2**30, 2)
            out["disk_%s_free" % s] = rnd(u.free / 2**30, 2)
            out["disk_%s_read_only" % s] = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
        except OSError as exc:
            log.warning("disk %s: %s", path, exc)
        part = self.partition_of(path)
        if part:
            errors = read_text("/sys/fs/ext4/%s/errors_count" % part)
            if errors is not None and errors.isdigit():
                out["disk_%s_fs_errors" % s] = int(errors)
        return out

    def _block_io(self):
        out = {}
        now = time.monotonic()
        current = {}
        for dev in self.block_devices:
            fields = (read_text("/sys/block/%s/stat" % dev) or "").split()
            if len(fields) >= 7:
                current[dev] = (int(fields[2]) * 512, int(fields[6]) * 512)
        last = self._last_io
        self._last_io = (now, current)
        for dev in self.block_devices:
            s = slug(dev)
            rd, wr = current.get(dev, (None, None))
            out["io_%s_written" % s] = rnd(wr / 2**30, 2) if wr is not None else None
            out["io_%s_read_rate" % s] = None
            out["io_%s_write_rate" % s] = None
            if last and dev in last[1] and rd is not None:
                dt = now - last[0]
                if dt > 0:
                    out["io_%s_read_rate" % s] = rnd(max(rd - last[1][dev][0], 0) / dt / 1000)
                    out["io_%s_write_rate" % s] = rnd(max(wr - last[1][dev][1], 0) / dt / 1000)
            lt = read_text("/sys/block/%s/device/life_time" % dev)
            if lt is not None:
                # eMMC: two estimates (type A/B), 0x01 = 0-10 % used ... 0x0B = exceeded
                try:
                    vals = [int(x, 16) for x in lt.split()]
                    out["io_%s_life_time" % s] = min(max(vals) * 10, 110) if max(vals) else None
                except ValueError:
                    out["io_%s_life_time" % s] = None
            eol = read_text("/sys/block/%s/device/pre_eol_info" % dev)
            if eol is not None:
                out["io_%s_pre_eol" % s] = {"0x01": "normal", "0x02": "warning",
                                            "0x03": "urgent"}.get(eol.lower(), eol)
        return out

    def _network(self):
        out = {}
        now = time.monotonic()
        counters = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        last = self._last_net
        self._last_net = (now, counters)
        for nic in self.nics:
            s = slug(nic)
            c = counters.get(nic)
            st = stats.get(nic)
            out["net_%s_up" % s] = bool(st.isup) if st else False
            out["net_%s_speed" % s] = st.speed if st and st.speed > 0 else None
            out["net_%s_rx_total" % s] = rnd(c.bytes_recv / 2**30, 2) if c else None
            out["net_%s_tx_total" % s] = rnd(c.bytes_sent / 2**30, 2) if c else None
            out["net_%s_errors" % s] = (c.errin + c.errout) if c else None
            out["net_%s_dropped" % s] = (c.dropin + c.dropout) if c else None
            out["net_%s_rx_rate" % s] = None
            out["net_%s_tx_rate" % s] = None
            if c and last and nic in last[1]:
                dt = now - last[0]
                p = last[1][nic]
                if dt > 0:
                    out["net_%s_rx_rate" % s] = rnd(max(c.bytes_recv - p.bytes_recv, 0) * 8 / dt / 1000)
                    out["net_%s_tx_rate" % s] = rnd(max(c.bytes_sent - p.bytes_sent, 0) * 8 / dt / 1000)
        return out

    @staticmethod
    def _throttled():
        raw = read_text("/sys/devices/platform/soc/soc:firmware/get_throttled")
        if raw is None:
            try:
                res = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                     text=True, timeout=5)
                raw = res.stdout.strip().split("=")[-1] if res.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                raw = None
        keys = ("pi_undervoltage", "pi_freq_capped", "pi_throttled",
                "pi_undervoltage_occurred", "pi_throttled_occurred", "pi_throttle_flags")
        if not raw:
            return dict.fromkeys(keys)
        try:
            v = int(raw, 16)
        except ValueError:
            return dict.fromkeys(keys)
        return {
            "pi_undervoltage": bool(v & 0x1),
            "pi_freq_capped": bool(v & 0x2),
            "pi_throttled": bool(v & 0x4),
            "pi_undervoltage_occurred": bool(v & 0x10000),
            "pi_throttled_occurred": bool(v & 0x40000),
            "pi_throttle_flags": hex(v),
        }

    def _updates(self, every):
        value, stamp = self._updates_cache
        if value is not None and time.monotonic() - stamp < every:
            return value
        if not os.path.exists("/usr/bin/apt-get"):
            return None
        # Simulation only - does not refresh package lists (the apt-daily timer does that).
        cmd = ["apt-get", "-s", "-o", "Debug::NoLocking=1",
               "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=", "dist-upgrade"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                 env=dict(os.environ, LC_ALL="C"))
            value = sum(1 for line in res.stdout.splitlines() if line.startswith("Inst "))
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("update check failed: %s", exc)
            value = None
        self._updates_cache = (value, time.monotonic())
        return value


# --------------------------------------------------------------------------
# MQTT / Home Assistant
# --------------------------------------------------------------------------
class Agent:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        g = cfg["general"]
        dev = cfg["device"]
        hostname = socket.gethostname().split(".")[0]
        self.node_id = slug(dev["node_id"] or hostname)
        self.device_name = dev["name"] or hostname
        self.base = "%s/%s" % (dev["base_topic"].strip("/"), self.node_id)
        self.state_topic = self.base + "/state"
        self.avail_topic = self.base + "/availability"
        self.prefix = cfg["homeassistant"]["discovery_prefix"].strip("/")
        self.status_topic = cfg["homeassistant"]["status_topic"]
        self.interval = max(5, g.getint("interval"))
        exp = g["expire_after"].strip().lower()
        self.expire_after = self.interval * 3 if exp == "auto" else int(exp or 0)
        self.lang = 1 if g["language"].strip().lower().startswith("de") else 0
        self.labels = getattr(cfg, "labels", {})
        self.flags_as_text = g["flags"].strip().lower() == "text"

        self.host = Host(cfg)
        self.entities = self.host.entities()
        if self.flags_as_text:
            for ent in self.entities:
                if ent.flag:
                    # enum sensor with "Yes"/"No" instead of HA's "OK"/"Problem"; the
                    # binary_sensor of older versions is removed on discovery
                    ent.legacy_component = ent.component
                    ent.component = "sensor"
                    ent.device_class = "enum"

        self.device = {
            "identifiers": ["host_metrics_%s" % self.node_id],
            "name": self.device_name,
            "model": self.host.model,
            "sw_version": self.host.os_name,
        }
        self.origin = {"name": "host-metrics-mqtt", "sw": __version__, "url": PROJECT_URL}

        self.stop = threading.Event()
        self.need_discovery = threading.Event()
        self.client = None

    # -- naming ------------------------------------------------------------
    def t(self, key):
        return TEXT[key][self.lang]

    def label(self, kind, name):
        configured = self.labels.get(kind, {}).get(name)
        if configured:
            return configured
        if kind == "disks":
            if name == "/":
                return self.t("label_root")
            if name in ("/boot", "/boot/firmware", "/boot/efi"):
                return self.t("label_boot")
        if kind == "block_devices" and re.match(r"mmcblk\d+$", name):
            sd_cards = [d for d in self.host.block_devices if re.match(r"mmcblk\d+$", d)]
            return self.t("label_sd") if len(sd_cards) == 1 else "%s %s" % (self.t("label_sd"), name)
        return name

    def name(self, ent):
        text = self.t(ent.text_key)
        if ent.arg is None:
            return text
        return text.format(self.label(ent.kind, ent.arg))

    def legacy_topic(self, ent):
        return "%s/%s/%s/%s/config" % (self.prefix, ent.legacy_component, self.node_id, ent.key)

    def discovery_topic(self, ent):
        return "%s/%s/%s/%s/config" % (self.prefix, ent.component, self.node_id, ent.key)

    def discovery_payload(self, ent):
        p = {
            "name": self.name(ent),
            "unique_id": "%s_%s" % (self.node_id, ent.key),
            "state_topic": self.state_topic,
            "availability_topic": self.avail_topic,
            "device": self.device,
            "origin": self.origin,
        }
        if ent.component == "binary_sensor":
            p["value_template"] = binary_template(ent.key)
        elif ent.flag:
            yes, no = self.t("yes"), self.t("no")
            p["value_template"] = ("{%% if value_json.%s is none %%}None{%% elif value_json.%s %%}"
                                   "%s{%% else %%}%s{%% endif %%}" % (ent.key, ent.key, yes, no))
            p["options"] = [yes, no]
        else:
            p["value_template"] = "{{ value_json.%s }}" % ent.key
        for k, v in (("unit_of_measurement", ent.unit), ("device_class", ent.device_class),
                     ("state_class", ent.state_class), ("icon", ent.icon),
                     ("entity_category", ent.category),
                     ("suggested_display_precision", ent.precision)):
            if v is not None:
                p[k] = v
        if ent.expire and self.expire_after and ent.component == "sensor":
            p["expire_after"] = self.expire_after
        return p

    # -- publishing --------------------------------------------------------
    def publish(self, topic, payload, retain=False):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        if self.dry_run:
            print("%s%s\n  %s" % (topic, "  [retain]" if retain else "", payload))
            return
        if self.client is not None:
            self.client.publish(topic, payload, qos=0 if not retain else 1, retain=retain)

    def publish_discovery(self):
        for component, key in REMOVED_ENTITIES:
            self.publish("%s/%s/%s/%s/config" % (self.prefix, component, self.node_id, key),
                         "", retain=True)
        for ent in self.entities:
            if ent.legacy_component:
                self.publish(self.legacy_topic(ent), "", retain=True)
            self.publish(self.discovery_topic(ent), self.discovery_payload(ent), retain=True)
        log.info("discovery published (%d entities)", len(self.entities))

    def remove_discovery(self):
        for ent in self.entities:
            if ent.legacy_component:
                self.publish(self.legacy_topic(ent), "", retain=True)
            self.publish(self.discovery_topic(ent), "", retain=True)
        self.publish(self.avail_topic, "", retain=True)
        log.info("removed %d discovery entries", len(self.entities))

    def cycle(self):
        state = self.host.collect()
        self.publish(self.state_topic, state)
        return state

    # -- MQTT connection ---------------------------------------------------
    def _make_client(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit("paho-mqtt is missing - install it: sudo apt install python3-paho-mqtt")
        c = self.cfg["mqtt"]
        client_id = c["client_id"] or "host-metrics-%s" % self.node_id
        try:  # paho-mqtt >= 2.0
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=client_id)

        password = c["password"]
        if c["password_file"]:
            password = read_text(c["password_file"]) or ""
        password = os.environ.get("HOST_METRICS_MQTT_PASSWORD", password)
        if c["username"]:
            client.username_pw_set(c["username"], password or None)
        if c.getboolean("tls"):
            client.tls_set(ca_certs=c["tls_ca"] or None)
            if c.getboolean("tls_insecure"):
                client.tls_insecure_set(True)

        client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=120)

        def on_connect(cl, userdata, flags, rc, properties=None):
            if rc != 0:
                log.error("MQTT connection refused: %s", rc)
                return
            log.info("connected to %s:%s", c["host"], c["port"])
            cl.subscribe(self.status_topic, qos=1)
            cl.publish(self.avail_topic, "online", qos=1, retain=True)
            self.need_discovery.set()

        def on_disconnect(cl, userdata, *args):
            if not self.stop.is_set():
                log.warning("MQTT connection lost - reconnecting")

        def on_message(cl, userdata, msg):
            if msg.topic == self.status_topic and msg.payload.decode(errors="ignore") == "online":
                log.info("Home Assistant restarted - republishing discovery")
                t = threading.Timer(random.uniform(2, 8), self.need_discovery.set)
                t.daemon = True
                t.start()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    def run(self):
        c = self.cfg["mqtt"]
        self.client = self._make_client()
        self.client.connect_async(c["host"], c.getint("port"), keepalive=c.getint("keepalive"))
        self.client.loop_start()
        next_run = 0.0
        try:
            while not self.stop.is_set():
                force = False
                if self.need_discovery.is_set():
                    self.need_discovery.clear()
                    self.publish_discovery()
                    force = True
                now = time.monotonic()
                if force or now >= next_run:
                    if self.client.is_connected():
                        try:
                            self.cycle()
                        except Exception:  # noqa: BLE001 - keep the service alive
                            log.exception("collecting metrics failed")
                    next_run = now + self.interval
                self.stop.wait(1)
        finally:
            if self.client.is_connected():
                info = self.client.publish(self.avail_topic, "offline", qos=1, retain=True)
                # poll instead of wait_for_publish(timeout=...), which paho < 1.6 lacks
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        if info.is_published():
                            break
                    except (RuntimeError, ValueError):
                        break
                    time.sleep(0.05)
            self.client.disconnect()
            self.client.loop_stop()
            log.info("stopped")

    def run_remove(self):
        c = self.cfg["mqtt"]
        self.client = self._make_client()
        self.client.on_connect = None
        self.client.connect(c["host"], c.getint("port"), keepalive=c.getint("keepalive"))
        self.client.loop_start()
        self.remove_discovery()
        time.sleep(2)
        self.stop.set()
        self.client.disconnect()
        self.client.loop_stop()


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Publish host metrics to Home Assistant via MQTT.")
    ap.add_argument("-c", "--config", help="path to the YAML config file "
                    "(default: $CREDENTIALS_DIRECTORY/config, /etc/host-metrics-mqtt/config.yaml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print discovery and one state message instead of sending them")
    ap.add_argument("--remove", action="store_true",
                    help="remove all entities of this host from Home Assistant and exit")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args()

    path = find_config(args.config)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    logging.basicConfig(level=cfg["general"]["log_level"].upper(),
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    log.info("host-metrics-mqtt %s, config: %s", __version__, path or "(defaults)")
    if not path:
        log.warning("no config file found - using defaults (broker on localhost)")

    agent = Agent(cfg, dry_run=args.dry_run)

    if args.dry_run:
        agent.publish_discovery()
        time.sleep(2)  # so rates and CPU usage have a measuring window
        agent.cycle()
        return 0
    if args.remove:
        agent.run_remove()
        return 0

    signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
    signal.signal(signal.SIGINT, lambda *_: agent.stop.set())
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
