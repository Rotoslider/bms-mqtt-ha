#!/usr/bin/env python3
"""
JBD/Xiaoxiang BMS  ->  MQTT (Home Assistant auto-discovery) bridge.

Polls a set of BLE LiFePO4 batteries (Vatrer 51.2V / JBD-protocol BMS) round-robin
over the Pi's onboard Bluetooth, reads pack info (0x03) and per-cell voltages (0x04),
and publishes everything to an MQTT broker with Home Assistant discovery so each
battery + every individual cell shows up automatically.

No cloud, no writes to the BMS -- read-only monitoring.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt

LOG = logging.getLogger("bms")

FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"  # notify (BMS -> us)
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"  # write  (us -> BMS)

CONFIG_PATH = os.environ.get("BMS_CONFIG", "/opt/bms-mqtt/config.json")


# ---------------------------------------------------------------------------
# JBD protocol helpers
# ---------------------------------------------------------------------------
def build_cmd(reg: int) -> bytes:
    """Read-register request: DD A5 <reg> 00 <chkH> <chkL> 77."""
    body = bytes([reg, 0x00])
    chk = (0x10000 - sum(body)) & 0xFFFF
    return bytes([0xDD, 0xA5]) + body + bytes([chk >> 8, chk & 0xFF, 0x77])


def u16(d, i):
    return int.from_bytes(d[i:i + 2], "big", signed=False)


def s16(d, i):
    return int.from_bytes(d[i:i + 2], "big", signed=True)


def frame_complete(buf: bytearray) -> bool:
    return len(buf) >= 4 and buf[0] == 0xDD and len(buf) >= 7 + buf[3]


def validate(frame: bytes) -> bool:
    """Verify start/end markers, status OK, and checksum over status+len+data."""
    if len(frame) < 7 or frame[0] != 0xDD or frame[-1] != 0x77:
        return False
    ln = frame[3]
    if len(frame) < 7 + ln or frame[2] != 0x00:
        return False
    chk = (0x10000 - sum(frame[2:4 + ln])) & 0xFFFF
    got = u16(frame, 4 + ln)
    return chk == got


def parse_basic(frame: bytes) -> dict:
    d = frame[4:4 + frame[3]]
    nntc = d[22]
    temps = [round((u16(d, 23 + 2 * i) - 2731) / 10.0, 1) for i in range(nntc)]
    fet = d[20]
    prot = u16(d, 16)
    out = {
        "voltage": round(u16(d, 0) / 100.0, 2),
        "current": round(s16(d, 2) / 100.0, 2),
        "remaining_ah": round(u16(d, 4) / 100.0, 2),
        "nominal_ah": round(u16(d, 6) / 100.0, 2),
        "cycles": u16(d, 8),
        "balance_bits": u16(d, 12) | (u16(d, 14) << 16),
        "protection_raw": prot,
        "soc": d[19],
        "num_cells": d[21],
        "fet_charge": "ON" if fet & 0x01 else "OFF",
        "fet_discharge": "ON" if fet & 0x02 else "OFF",
    }
    out["power"] = round(out["voltage"] * out["current"], 1)
    for i, t in enumerate(temps):
        out[f"temp_{i + 1}"] = t
    if temps:
        out["temp_max"] = max(temps)
        out["temp_min"] = min(temps)
    out["balancing"] = "ON" if out["balance_bits"] else "OFF"
    out["protection"] = decode_protection(prot)
    out["problem"] = "ON" if prot else "OFF"
    return out


PROT_BITS = [
    "Cell overvolt", "Cell undervolt", "Pack overvolt", "Pack undervolt",
    "Charge overtemp", "Charge undertemp", "Discharge overtemp", "Discharge undertemp",
    "Charge overcurrent", "Discharge overcurrent", "Short circuit", "IC error",
    "FET locked",
]


def decode_protection(bits: int) -> str:
    if not bits:
        return "None"
    flags = [name for i, name in enumerate(PROT_BITS) if bits & (1 << i)]
    return ", ".join(flags) if flags else f"0x{bits:04X}"


def parse_cells(frame: bytes) -> dict:
    d = frame[4:4 + frame[3]]
    n = frame[3] // 2
    mv = [u16(d, 2 * i) for i in range(n)]
    out = {f"cell_{i + 1:02d}": round(v / 1000.0, 3) for i, v in enumerate(mv)}
    if mv:
        out["cell_min_mv"] = min(mv)
        out["cell_max_mv"] = max(mv)
        out["cell_avg_mv"] = round(sum(mv) / len(mv), 1)
        out["cell_delta_mv"] = max(mv) - min(mv)
        out["cell_min_no"] = mv.index(min(mv)) + 1
        out["cell_max_no"] = mv.index(max(mv)) + 1
    return out


# ---------------------------------------------------------------------------
# BLE read
# ---------------------------------------------------------------------------
async def read_battery(mac: str, poll: dict) -> dict:
    buf = bytearray()
    ev = asyncio.Event()

    def cb(_sender, data):
        buf.extend(data)
        if frame_complete(buf):
            ev.set()

    async def query(reg: int) -> bytes:
        buf.clear()
        ev.clear()
        await client.write_gatt_char(FF02, build_cmd(reg), response=False)
        await asyncio.wait_for(ev.wait(), poll["notify_timeout"])
        frame = bytes(buf[:7 + buf[3]])
        if not validate(frame):
            raise ValueError(f"bad frame for reg 0x{reg:02x}: {frame.hex()}")
        return frame

    # Discover the device fresh (refreshes BlueZ cache). If it isn't advertising
    # right now (e.g. a stale connection is holding it), fall back to connecting
    # by address, which can still attach via BlueZ's device cache.
    device = await BleakScanner.find_device_by_address(
        mac, timeout=poll.get("find_timeout", 8))
    target = device if device is not None else mac

    client = BleakClient(target, timeout=poll["connect_timeout"])
    await client.connect()
    try:
        await asyncio.sleep(poll["settle_seconds"])  # BMS needs a moment before notifications flow
        await client.start_notify(FF01, cb)
        data = {}
        data.update(parse_basic(await query(0x03)))
        data.update(parse_cells(await query(0x04)))
        return data
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def read_battery_retry(mac: str, poll: dict, name: str) -> dict:
    attempts = poll.get("attempts", 3)
    last = None
    for i in range(attempts):
        try:
            return await read_battery(mac, poll)
        except Exception as e:
            last = e
            LOG.info("[%s] attempt %d/%d failed: %s", name, i + 1, attempts,
                     repr(e) if str(e) else type(e).__name__)
            await asyncio.sleep(poll.get("retry_seconds", 3))
    raise last if last else RuntimeError("unknown read failure")


# ---------------------------------------------------------------------------
# Home Assistant discovery
# ---------------------------------------------------------------------------
def sensor_defs(num_cells: int):
    """Return list of (component, key, name, unit, device_class, state_class, extra)."""
    S = "measurement"
    defs = [
        ("sensor", "voltage", "Voltage", "V", "voltage", S, {}),
        ("sensor", "current", "Current", "A", "current", S, {}),
        ("sensor", "power", "Power", "W", "power", S, {}),
        ("sensor", "soc", "SOC", "%", "battery", S, {}),
        ("sensor", "remaining_ah", "Remaining capacity", "Ah", None, S, {"icon": "mdi:battery-50"}),
        ("sensor", "nominal_ah", "Nominal capacity", "Ah", None, None, {"icon": "mdi:battery", "entity_category": "diagnostic"}),
        ("sensor", "cycles", "Cycles", None, None, "total_increasing", {"icon": "mdi:battery-sync"}),
        ("sensor", "cell_min_mv", "Cell min", "mV", "voltage", S, {}),
        ("sensor", "cell_max_mv", "Cell max", "mV", "voltage", S, {}),
        ("sensor", "cell_avg_mv", "Cell avg", "mV", "voltage", S, {}),
        ("sensor", "cell_delta_mv", "Cell delta", "mV", "voltage", S, {"icon": "mdi:battery-alert"}),
        ("sensor", "cell_min_no", "Lowest cell #", None, None, None, {"icon": "mdi:numeric", "entity_category": "diagnostic"}),
        ("sensor", "cell_max_no", "Highest cell #", None, None, None, {"icon": "mdi:numeric", "entity_category": "diagnostic"}),
        ("sensor", "protection", "Protection", None, None, None, {"icon": "mdi:shield-alert"}),
        ("binary_sensor", "problem", "Problem", None, "problem", None, {}),
        ("binary_sensor", "fet_charge", "Charge FET", None, "power", None, {"entity_category": "diagnostic"}),
        ("binary_sensor", "fet_discharge", "Discharge FET", None, "power", None, {"entity_category": "diagnostic"}),
        ("binary_sensor", "balancing", "Balancing", None, None, None, {"icon": "mdi:scale-balance"}),
    ]
    # temps (assume up to 3; only published ones get values, others stay unavailable harmlessly)
    for i in range(1, 4):
        defs.append(("sensor", f"temp_{i}", f"Temp {i}", "°C", "temperature", S, {}))
    defs.append(("sensor", "temp_max", "Temp max", "°C", "temperature", S, {}))
    defs.append(("sensor", "temp_min", "Temp min", "°C", "temperature", S, {}))
    # per-cell voltages
    for i in range(1, num_cells + 1):
        defs.append(("sensor", f"cell_{i:02d}", f"Cell {i:02d}", "V", "voltage", S,
                     {"suggested_display_precision": 3}))
    return defs


def publish_discovery(client, cfg, batt, num_cells):
    base = cfg["mqtt"]["base_topic"]
    disc = cfg["mqtt"]["discovery_prefix"]
    bid = batt["bid"]
    # manufacturer/model shown on the HA device page. Resolve per-battery override,
    # then a global default in config, then the tested-hardware fallback.
    defaults = cfg.get("device_defaults", {})
    manufacturer = batt.get("manufacturer") or defaults.get("manufacturer") or "Vatrer"
    model = batt.get("model") or defaults.get("model") or "51.2V 100Ah (JBD BMS)"
    serial = batt.get("serial", "")
    # NOTE: the "vatrer_" id prefix is an opaque, non-user-visible unique_id namespace.
    # It stays constant on purpose so existing HA devices aren't orphaned/duplicated
    # when manufacturer/model change — it is NOT the displayed brand.
    device = {
        "identifiers": [f"vatrer_{bid}"],
        "name": batt["name"],
        "manufacturer": manufacturer,
        "model": f"{model} {serial}".strip(),
    }
    state_topic = f"{base}/{bid}/state"
    avail = [
        {"topic": f"{base}/{bid}/availability"},
        {"topic": f"{base}/bridge/status"},
    ]
    for component, key, name, unit, dclass, sclass, extra in sensor_defs(num_cells):
        payload = {
            "name": name,
            "unique_id": f"vatrer_{bid}_{key}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": device,
            "availability": avail,
            "availability_mode": "all",
        }
        if component == "binary_sensor":
            payload["payload_on"] = "ON"
            payload["payload_off"] = "OFF"
        if unit:
            payload["unit_of_measurement"] = unit
        if dclass:
            payload["device_class"] = dclass
        if sclass:
            payload["state_class"] = sclass
        payload.update(extra)
        topic = f"{disc}/{component}/vatrer_{bid}/{key}/config"
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
    LOG.info("[%s] published discovery (%d cells)", batt["name"], num_cells)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for b in cfg["batteries"]:
        b["bid"] = b["mac"].replace(":", "").lower()[-6:]
    return cfg


async def run():
    cfg = load_config()
    m = cfg["mqtt"]
    poll = cfg["poll"]
    base = m["base_topic"]

    cli = mqtt.Client(client_id="bms-bridge")
    if m.get("username"):
        cli.username_pw_set(m["username"], m.get("password"))
    cli.will_set(f"{base}/bridge/status", "offline", qos=1, retain=True)

    def on_connect(client, userdata, flags, rc):
        # Fires on first connect AND every auto-reconnect (e.g. after a broker
        # restart) -> re-assert that the bridge is online.
        client.publish(f"{base}/bridge/status", "online", qos=1, retain=True)
        LOG.info("MQTT connected (rc=%s)", rc)

    cli.on_connect = on_connect
    cli.connect(m["host"], m["port"], keepalive=60)
    cli.loop_start()
    LOG.info("MQTT connecting to %s:%s", m["host"], m["port"])

    discovered = set()      # bids we've sent discovery for

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        cycle_start = time.monotonic()
        ok = 0

        for batt in cfg["batteries"]:
            if stop.is_set():
                break
            bid = batt["bid"]
            try:
                data = await read_battery_retry(batt["mac"], poll, batt["name"])
                if bid not in discovered:
                    publish_discovery(cli, cfg, batt, data.get("num_cells", 16))
                    discovered.add(bid)
                cli.publish(f"{base}/{bid}/state", json.dumps(data), qos=0, retain=True)
                cli.publish(f"{base}/{bid}/availability", "online", qos=1, retain=True)
                ok += 1
                LOG.info("[%s] %.2fV %.2fA SOC %d%% | cell %d-%dmV d=%dmV t=%s",
                         batt["name"], data["voltage"], data["current"], data["soc"],
                         data["cell_min_mv"], data["cell_max_mv"], data["cell_delta_mv"],
                         data.get("temp_max"))
            except Exception as e:
                LOG.warning("[%s] read failed: %s", batt["name"],
                            repr(e) if str(e) else type(e).__name__)
                cli.publish(f"{base}/{bid}/availability", "offline", qos=1, retain=True)
            await asyncio.sleep(poll.get("gap_seconds", 1.5))

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1.0, poll["cycle_seconds"] - elapsed)
        LOG.info("cycle done: %d/%d ok in %.1fs, sleeping %.1fs",
                 ok, len(cfg["batteries"]), elapsed, sleep_for)
        try:
            await asyncio.wait_for(stop.wait(), sleep_for)
        except asyncio.TimeoutError:
            pass

    cli.publish(f"{base}/bridge/status", "offline", qos=1, retain=True)
    cli.loop_stop()
    cli.disconnect()
    LOG.info("stopped cleanly")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
