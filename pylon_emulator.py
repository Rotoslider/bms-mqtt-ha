#!/usr/bin/env python3
"""
Pylontech LV RS485 battery emulator.

Presents the 8 parallel packs (read by bms_mqtt.py and published to MQTT) as ONE
virtual Pylontech battery to a Sungold/SRNE-class inverter set to BMS protocol PYL.
Answers the inverter's 6x-branch polls (CID2 0x61 analog, 0x62 alarms, 0x63
charge/discharge management). Frame layout is replicated byte-for-byte from the
reference emulator Fahmula/esphome-pylontech-rs485 (verified against SRNE).

SAFETY:
  * Read-only toward the batteries; only WRITES to the inverter's RS485 BMS port.
  * `listen_only` (config) logs what it WOULD send without transmitting — use it to
    sniff the inverter's polls / confirm wiring before going live.
  * If too few packs have fresh data, it stops responding (fail-safe): the inverter
    raises a BMS-comms fault and halts charge/discharge rather than acting on stale data.
  * Charge/discharge limits are derived conservatively from the WORST cell and worst
    temperature across all packs. Review the thresholds in config.json["emulator"].
  * Optional SOC recalibration (recal_enable) periodically raises CVL so every
    pack's coulomb counter re-zeros at true full — see docs/CLOSED-LOOP.md §11.

Run:
  python3 pylon_emulator.py --selftest      # validate codec, print sample frames, no serial
  python3 pylon_emulator.py                 # run (honors listen_only in config)
"""
import argparse, json, logging, os, sys, threading, time
import paho.mqtt.client as mqtt

LOG = logging.getLogger("pylon")
CONFIG_PATH = os.environ.get("BMS_CONFIG", "/opt/bms-mqtt/config.json")
SOI, EOI = 0x7E, 0x0D
VER, ADR, CID1 = 0x20, 0x02, 0x46   # response constants (CID2 in responses is always 00)


# ---------------------------------------------------------------------------
# Pylontech frame codec (matches reference exactly)
# ---------------------------------------------------------------------------
def length_field(info_len: int) -> bytes:
    n = ((info_len >> 8) & 0xF) + ((info_len >> 4) & 0xF) + (info_len & 0xF)
    lchk = (~n & 0x0F) + 1
    return b"%1X%03X" % (lchk & 0xF, info_len)


def checksum(frame_data: bytes) -> bytes:
    s = sum(frame_data) & 0xFFFF
    return b"%04X" % ((~s + 1) & 0xFFFF)


def build_response(info_ascii: bytes) -> bytes:
    """Wrap an INFO payload (ASCII-hex bytes) in a full response frame."""
    frame_data = b"%02X%02X" % (VER, ADR) + b"4600" + length_field(len(info_ascii)) + info_ascii
    return bytes([SOI]) + frame_data + checksum(frame_data) + bytes([EOI])


def decode_frame(frame: bytes) -> dict:
    """Parse a raw frame (SOI..EOI). Returns dict incl. cid2 hex and checksum validity."""
    body = frame[1:-1]            # strip SOI / EOI
    data, chk = body[:-4], body[-4:]
    valid = checksum(data) == chk
    out = {"valid": valid, "raw": frame.decode("ascii", "replace")}
    if len(data) >= 8:
        out["ver"] = data[0:2].decode()
        out["adr"] = data[2:4].decode()
        out["cid1"] = data[4:6].decode()
        out["cid2"] = data[6:8].decode()
    return out


def u16(v) -> bytes:
    return b"%04X" % (int(round(v)) & 0xFFFF)


def u8(v) -> bytes:
    return b"%02X" % (int(round(v)) & 0xFF)


# ---------------------------------------------------------------------------
# Aggregation of the 8 packs into one virtual battery
# ---------------------------------------------------------------------------
class Aggregator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.packs = {}          # bid -> (timestamp, state dict)
        self.lock = threading.Lock()

    def update(self, bid, state):
        with self.lock:
            self.packs[bid] = (time.monotonic(), state)

    def fresh(self):
        win = self.cfg["freshness_seconds"]
        now = time.monotonic()
        with self.lock:
            return [s for (ts, s) in self.packs.values() if now - ts <= win]

    def virtual(self):
        """Return aggregated virtual-battery dict, or None if not enough fresh data."""
        packs = self.fresh()
        if len(packs) < self.cfg["min_fresh_batteries"]:
            return None

        def vals(key, default=None):
            return [p[key] for p in packs if key in p and p[key] is not None] or \
                   ([default] if default is not None else [])

        v = vals("voltage"); cur = vals("current"); soc = vals("soc")
        cmax = vals("cell_max_mv"); cmin = vals("cell_min_mv")
        tmax = vals("temp_max"); tmin = vals("temp_min")
        cyc = vals("cycles", 0)
        rem = vals("remaining_ah", 0); nom = vals("nominal_ah", 0)
        problems = [p.get("problem") == "ON" for p in packs]
        # Per-pack charge/discharge capability, straight from each BMS's own FET state.
        # A pack whose BMS opened its charge FET has finished or is protecting itself:
        # it drops out of the charge circuit, but the REST of the bank keeps charging.
        # The bank is only truly "blocked" when NO pack can accept charge anymore. This
        # is what lets a leader pack top out and coast at ~0 A while the laggards keep
        # filling, instead of the first full pack vetoing charge for all eight.
        chg_caps = [p for p in packs if p.get("fet_charge", "ON") == "ON"]
        dis_caps = [p for p in packs if p.get("fet_discharge", "ON") == "ON"]

        def worst(pack_list, key, agg):
            xs = [p[key] for p in pack_list if p.get(key) is not None]
            return agg(xs) if xs else None

        if not (v and cur and soc and cmax and cmin and tmax and tmin):
            return None
        temps = tmax + tmin
        return {
            "voltage": sum(v) / len(v),
            "current": sum(cur),                      # parallel banks add
            # Parallel packs sit on one bus, so their TRUE SOC is identical.
            # Coulomb counters only drift DOWN between full-charge resets, so the
            # highest (least-drifted) counter is the honest bank estimate; the
            # mean drags in every stale counter and decays over time.
            "soc": max(soc),
            "soc_mean": round(sum(soc) / len(soc)),
            "soc_min": min(soc),
            "cell_max_mv": max(cmax),                 # true worst cell across ALL packs
            "cell_min_mv": min(cmin),                 #   (reported to inverter + hard safety)
            "cell_max_chg_mv": worst(chg_caps, "cell_max_mv", max),  # worst cell still charging
            "cell_min_dis_mv": worst(dis_caps, "cell_min_mv", min),  # worst cell still discharging
            "temp_max": max(tmax),
            "temp_min": min(tmin),
            "temp_avg": sum(temps) / len(temps),
            "cycles": max(cyc),
            "remaining_ah": sum(rem),
            "nominal_ah": sum(nom),
            "n_fresh": len(packs),
            "n_charge_capable": len(chg_caps),
            "n_discharge_capable": len(dis_caps),
            "any_problem": any(problems),             # surfaced as an alarm, not a bank-wide stop
            "charge_blocked": len(chg_caps) == 0,     # blocked only when NO pack can charge
            "discharge_blocked": len(dis_caps) == 0,
        }


# ---------------------------------------------------------------------------
# SOC recalibration: periodic elevated absorption so every pack's coulomb
# counter re-zeros.
# ---------------------------------------------------------------------------
class RecalController:
    """JBD coulomb counters re-zero (SOC := 100) only when the BMS sees its
    full-charge condition: max cell at/above its 'cell full voltage' with the
    charge current tapered off. The everyday CVL (55.2 V = 3.45 V/cell) never
    reaches that, so each counter ratchets down a little every cycle and the
    reported SOC decays even though the bank is physically full.

    Fix: when any pack's counter has drifted below `recal_trigger_soc` and the
    bank is already sitting at the top of charge, raise CVL to
    `recal_cvl_volts` and hold until every pack's BMS snaps back to full
    (watched live via each pack's reported SOC), then drop back to normal.
    Guardrails: abort if the highest cell reaches `recal_abort_cell_mv`
    (below the `cell_overvolt_mv` hard stop), give up after
    `recal_timeout_minutes`, and never attempt more than once per
    `recal_cooldown_hours`. The result of the last attempt is persisted so a
    service restart doesn't retrigger immediately.
    """

    def __init__(self, e):
        self.enabled = bool(e.get("recal_enable", False))
        self.cvl = float(e.get("recal_cvl_volts", 56.8))
        self.trigger_soc = e.get("recal_trigger_soc", 97)
        self.done_soc = e.get("recal_done_soc", 99)
        self.entry_volts = e.get("recal_entry_volts", 54.8)
        self.entry_min_amps = e.get("recal_entry_min_amps", -1.0)
        self.abort_cell_mv = e.get("recal_abort_cell_mv", 3580)
        # SOC ceiling reported while a hold is active: an inverter that has
        # latched "battery full" at SOC 100 never re-enters a charge stage,
        # no matter what CVL we request. Must be below the inverter's own
        # re-charge threshold (the tested SRNE ignored 97).
        self.report_soc = e.get("recal_report_soc", 90)
        self.timeout_minutes = e.get("recal_timeout_minutes", 180)
        # Short enough that a dusk timeout re-arms before the next morning's
        # top-of-charge; the trigger condition itself keeps successes ~weekly.
        self.cooldown_hours = e.get("recal_cooldown_hours", 12)
        self.state_path = e.get("recal_state_path", "/opt/bms-mqtt/recal_state.json")
        self.active = False
        self.active_since = 0.0                       # monotonic
        try:
            with open(self.state_path) as f:
                self.state = json.load(f)             # last_attempt / last_result
        except Exception:
            self.state = {}

    def _finish(self, result):
        LOG.warning("RECAL %s after %.0f min -> CVL back to normal",
                    result, (time.monotonic() - self.active_since) / 60)
        self.active = False
        self.state = {"last_attempt": time.time(), "last_result": result}
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.state, f)
        except Exception as ex:
            LOG.warning("recal: cannot persist state to %s: %s", self.state_path, ex)

    def evaluate(self, va):
        """Advance the state machine. Returns the CVL override while a
        recalibration hold is active, else None (use the normal CVL)."""
        if not self.enabled:
            return None
        if self.active:
            if va["cell_max_mv"] >= self.abort_cell_mv:
                self._finish("ABORT (cell %d mV)" % va["cell_max_mv"])
            elif va["soc_min"] >= self.done_soc:
                self._finish("SUCCESS (every pack >= %d%%)" % self.done_soc)
            elif time.monotonic() - self.active_since > self.timeout_minutes * 60:
                self._finish("TIMEOUT (packs still below %d%%)" % self.done_soc)
            else:
                return self.cvl
            return None
        # Idle: start only when a counter has drifted AND the bank is already at
        # the top of charge (voltage near CVL, not actively discharging) AND the
        # cooldown since the previous attempt has passed.
        if va["soc_min"] > self.trigger_soc:
            return None
        if time.time() - self.state.get("last_attempt", 0) < self.cooldown_hours * 3600:
            return None
        if va["voltage"] < self.entry_volts or va["current"] < self.entry_min_amps:
            return None
        self.active = True
        self.active_since = time.monotonic()
        LOG.warning("RECAL START: CVL -> %.1f V until every pack SOC >= %d%% "
                    "(min now %d%%); abort at cell %d mV, timeout %d min",
                    self.cvl, self.done_soc, va["soc_min"],
                    self.abort_cell_mv, self.timeout_minutes)
        return self.cvl

    def status(self):
        if not self.enabled:
            return "off"
        if self.active:
            return "ACTIVE %dmin" % ((time.monotonic() - self.active_since) / 60)
        return "idle (last: %s)" % self.state.get("last_result", "never run")


# ---------------------------------------------------------------------------
# The "brain": derive conservative limits from the worst cell / temp
# ---------------------------------------------------------------------------
def compute_limits(va, e, cvl_override=None):
    """va = virtual battery dict, e = emulator config. Returns a limits dict.
    cvl_override (from RecalController) replaces cvl_volts during a recal hold."""
    # True extremes across every pack -> used for the HARD safety cutoffs and for
    # what we report to the inverter.
    cmax, cmin = va["cell_max_mv"], va["cell_min_mv"]
    tmax, tmin = va["temp_max"], va["temp_min"]
    # The current TAPER reads the worst cell among packs that are STILL charging /
    # discharging. A pack that has topped out (its BMS opened the charge FET) no longer
    # drags the whole bank's charge current down, so packs that are still low keep
    # receiving current until they finish too. Fall back to the global extreme if
    # capability data is unavailable (older payloads / self-test).
    cmax_chg = va.get("cell_max_chg_mv") or cmax
    cmin_dis = va.get("cell_min_dis_mv") or cmin

    def taper(x, x0, x1, y0, y1):
        if x <= min(x0, x1):
            return y0 if x0 < x1 else y1
        if x >= max(x0, x1):
            return y1 if x0 < x1 else y0
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    # Charge current: full until taper_start, ramp to ccl_min by taper_end
    ccl = taper(cmax_chg, e["cell_chg_taper_start_mv"], e["cell_chg_taper_end_mv"],
                e["ccl_max_amps"], e["ccl_min_amps"])
    # Discharge current: full until dis_taper_start (falling), ramp down to dcl_min
    dcl = taper(cmin_dis, e["cell_dis_taper_start_mv"], e["cell_dis_taper_end_mv"],
                e["dcl_max_amps"], e["dcl_min_amps"])

    charge_ok = True
    discharge_ok = True
    # Hard safety backstops act on the TRUE worst cell / temp across all packs.
    if cmax >= e["cell_overvolt_mv"]:
        charge_ok = False
    if cmin <= e["cell_undervolt_mv"]:
        discharge_ok = False
    if not (e["charge_temp_min_c"] <= tmin and tmax <= e["charge_temp_max_c"]):
        charge_ok = False
    if not (e["discharge_temp_min_c"] <= tmin and tmax <= e["discharge_temp_max_c"]):
        discharge_ok = False
    # Stop the BANK only when NO pack can take charge anymore (every charge FET open).
    # A single pack finishing no longer vetoes the rest -- each BMS manages its own pack.
    # (A per-pack `problem` is surfaced as an alarm in info_62, not a bank-wide halt.)
    if va["charge_blocked"]:
        charge_ok = False
    if va["discharge_blocked"]:
        discharge_ok = False

    if not charge_ok:
        ccl = 0.0
    if not discharge_ok:
        dcl = 0.0

    return {
        "cvl_v": cvl_override if cvl_override is not None else e["cvl_volts"],
        "dvl_v": e["dvl_volts"],
        "ccl_a": max(0.0, ccl),
        "dcl_a": max(0.0, dcl),
        "charge_enable": charge_ok,
        "discharge_enable": discharge_ok,
        # recal_force_charge: also assert the "charge immediately" bit during a
        # hold, for inverters that ignore the full-charge-request bit. NOTE:
        # depending on charger-source-priority this may pull grid power.
        "force_charge": va["soc"] <= e["force_charge_soc"] or
                        (cvl_override is not None and e.get("recal_force_charge", False)),
        # Pylontech "request full charge" bit: how a real battery asks the
        # inverter for a balancing/top-up charge. Set for the whole recal hold.
        "full_charge_request": cvl_override is not None,
    }


# ---------------------------------------------------------------------------
# Response builders (CID2 0x61 / 0x62 / 0x63), payloads per reference source
# ---------------------------------------------------------------------------
def dk(celsius):                      # deci-Kelvin
    return int(round((celsius + 273.15) * 10))


def info_61(va):
    t = dk(va["temp_avg"]); tmx = dk(va["temp_max"]); tmn = dk(va["temp_min"])
    cur_ca = int(round(va["current"] * 100)) & 0xFFFF      # centi-amps, 2's-comp
    fields = [
        u16(va["voltage"] * 1000), u16(cur_ca), u8(va["soc"]),
        u16(va["cycles"]), u16(va["cycles"]), u8(100), u8(100),     # cycles x2, SOH x2
        u16(va["cell_max_mv"]), u16(0x0101), u16(va["cell_min_mv"]), u16(0x0108),
        u16(t), u16(tmx), u16(0x0103), u16(tmn), u16(0x0109),       # cell temps
        u16(t), u16(tmx), u16(0x0101), u16(tmn), u16(0x0101),       # mosfet temps
        u16(t), u16(tmx), u16(0x0101), u16(tmn), u16(0x0101),       # bms temps
    ]
    return b"".join(fields)


def info_62(va, e):
    a1 = a2 = p1 = p2 = 0
    cmax, cmin = va["cell_max_mv"], va["cell_min_mv"]
    if cmax >= e["cell_overvolt_mv"] - 50:
        a1 |= (1 << 5)                                   # cell voltage high alarm
    if cmin <= e["cell_undervolt_mv"] + 50:
        a1 |= (1 << 4)                                   # cell voltage low alarm
    if va["temp_max"] >= e["charge_temp_max_c"]:
        a1 |= (1 << 3)                                   # cell temp high alarm
    if va["temp_min"] <= e["charge_temp_min_c"]:
        a1 |= (1 << 2)                                   # cell temp low alarm
    if cmax >= e["cell_overvolt_mv"]:
        p1 |= (1 << 5)                                   # cell overvoltage protection
    if cmin <= e["cell_undervolt_mv"]:
        p1 |= (1 << 4)                                   # cell undervoltage protection
    if va["any_problem"]:
        p2 |= (1 << 3)                                   # system fault
    return u8(a1) + u8(a2) + u8(p1) + u8(p2)


def info_63(lim):
    status = 0
    if lim["charge_enable"]:
        status |= (1 << 7)
    if lim["discharge_enable"]:
        status |= (1 << 6)
    if lim["force_charge"]:
        status |= (1 << 5)
    if lim.get("full_charge_request"):
        status |= (1 << 3)
    return (u16(lim["cvl_v"] * 1000) + u16(lim["dvl_v"] * 1000) +
            u16(lim["ccl_a"] * 10) + u16(lim["dcl_a"] * 10) + u8(status))


# ---------------------------------------------------------------------------
# Serial loop
# ---------------------------------------------------------------------------
def run(cfg):
    import serial
    e = cfg["emulator"]
    agg = Aggregator(e)
    recal = RecalController(e)

    # MQTT: subscribe to per-pack state
    base = cfg["mqtt"]["base_topic"]
    cli = mqtt.Client(client_id="pylon-emulator")
    if cfg["mqtt"].get("username"):
        cli.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password"))

    def on_connect(c, u, f, rc):
        c.subscribe(f"{base}/+/state")
        LOG.info("MQTT connected (rc=%s), subscribed %s/+/state", rc, base)

    def on_message(c, u, msg):
        try:
            bid = msg.topic.split("/")[1]
            if bid == "emulator":     # our own status publish, not a pack
                return
            agg.update(bid, json.loads(msg.payload))
        except Exception as ex:
            LOG.debug("bad msg on %s: %s", msg.topic, ex)

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(cfg["mqtt"]["host"], cfg["mqtt"]["port"], keepalive=60)
    cli.loop_start()

    listen_only = e.get("listen_only", True)
    ser = serial.Serial(e["serial_port"], e["baud"], timeout=0.2)
    LOG.info("Serial open %s @ %d 8N1 | listen_only=%s", e["serial_port"], e["baud"], listen_only)
    LOG.warning("LISTEN-ONLY: logging polls, NOT transmitting." if listen_only
                else "LIVE: responding to inverter polls.")

    buf = bytearray()
    last_summary = 0
    while True:
        chunk = ser.read(256)
        if chunk:
            for byte in chunk:
                if byte == SOI:
                    buf.clear(); buf.append(byte)
                elif buf:
                    buf.append(byte)
                    if byte == EOI:
                        handle_frame(bytes(buf), ser, agg, e, listen_only, recal)
                        buf.clear()
        # periodic state summary
        now = time.monotonic()
        if now - last_summary > 30:
            last_summary = now
            va = agg.virtual()
            if va:
                cvl_over = recal.evaluate(va)
                soc_reported = min(va["soc"], recal.report_soc) \
                    if cvl_over is not None else va["soc"]
                lim = compute_limits(va, e, cvl_over)
                LOG.info("virtual: %.2fV %.1fA SOC%d%% (mean%d min%d) cell %d-%dmV "
                         "t%.0f-%.0fC | CVL %.1f CCL %.0f DCL %.0f chg=%d dis=%d "
                         "(packs=%d chgcap=%d) recal=%s",
                         va["voltage"], va["current"], va["soc"], va["soc_mean"],
                         va["soc_min"], va["cell_min_mv"], va["cell_max_mv"],
                         va["temp_min"], va["temp_max"], lim["cvl_v"],
                         lim["ccl_a"], lim["dcl_a"], lim["charge_enable"],
                         lim["discharge_enable"], va["n_fresh"],
                         va["n_charge_capable"], recal.status())
                # NOTE: topic must NOT match the {base}/+/state subscription above,
                # or the emulator ingests its own summary as a phantom pack.
                cli.publish(f"{base}/emulator/status", json.dumps({
                    "soc": va["soc"], "soc_reported": soc_reported,
                    "soc_mean": va["soc_mean"],
                    "soc_min": va["soc_min"], "voltage": round(va["voltage"], 2),
                    "current": round(va["current"], 1), "cvl_v": lim["cvl_v"],
                    "ccl_a": round(lim["ccl_a"]), "dcl_a": round(lim["dcl_a"]),
                    "n_fresh": va["n_fresh"],
                    "n_charge_capable": va["n_charge_capable"],
                    "recal": recal.status(),
                }), retain=True)
            else:
                LOG.warning("virtual: NO FRESH DATA -> would fail-safe (not respond)")


def handle_frame(frame, ser, agg, e, listen_only, recal):
    d = decode_frame(frame)
    cid2 = d.get("cid2", "??")
    LOG.info("RX %s cid2=%s valid=%s", d["raw"].strip(), cid2, d.get("valid"))
    if not d.get("valid"):
        LOG.warning("  bad checksum -> ignoring")
        return
    if cid2 not in ("61", "62", "63"):
        LOG.info("  cid2 %s not handled -> ignoring", cid2)
        return

    va = agg.virtual()
    if va is None:
        LOG.warning("  no fresh battery data -> NOT responding (fail-safe)")
        return
    cvl_over = recal.evaluate(va)
    if cvl_over is not None:
        va = dict(va, soc=min(va["soc"], recal.report_soc))
    lim = compute_limits(va, e, cvl_over)
    if cid2 == "61":
        resp = build_response(info_61(va))
    elif cid2 == "62":
        resp = build_response(info_62(va, e))
    else:
        resp = build_response(info_63(lim))
        LOG.info("  -> CID63 CVL=%.1fV CCL=%.0fA DCL=%.0fA chg=%d dis=%d force=%d",
                 lim["cvl_v"], lim["ccl_a"], lim["dcl_a"], lim["charge_enable"],
                 lim["discharge_enable"], lim["force_charge"])
    if listen_only:
        LOG.info("  WOULD SEND: %s", resp.decode("ascii", "replace").strip())
    else:
        ser.write(resp)
        LOG.info("  TX %s", resp.decode("ascii", "replace").strip())


# ---------------------------------------------------------------------------
def selftest():
    # 1) Verify checksum/length against the two known-good frames from the spec.
    assert checksum(b"0002464F0000") == b"FD9A", checksum(b"0002464F0000")
    assert checksum(b"20024600E00200") == b"FD3B", checksum(b"20024600E00200")
    assert length_field(2) == b"E002", length_field(2)
    assert length_field(0) == b"0000", length_field(0)
    print("codec self-test: PASS (checksums + length field match spec)")

    # 2) Build a sample 0x63 with a representative virtual battery + limits.
    e = json.load(open(CONFIG_PATH))["emulator"]
    va = {"voltage": 53.2, "current": 12.5, "soc": 90, "soc_mean": 88, "soc_min": 80,
          "cell_max_mv": 3360,
          "cell_min_mv": 3300, "cell_max_chg_mv": 3360, "cell_min_dis_mv": 3300,
          "temp_max": 28.0, "temp_min": 25.0, "temp_avg": 26.5,
          "cycles": 20, "remaining_ah": 720, "nominal_ah": 800, "n_fresh": 8,
          "n_charge_capable": 8, "n_discharge_capable": 8,
          "any_problem": False, "charge_blocked": False, "discharge_blocked": False}
    lim = compute_limits(va, e)
    for cid, payload in (("61", info_61(va)), ("62", info_62(va, e)), ("63", info_63(lim))):
        fr = build_response(payload)
        d = decode_frame(fr)
        assert d["valid"], f"self-built {cid} failed its own checksum!"
        print(f"sample CID{cid} frame: {fr.decode().strip()}  (valid={d['valid']})")
    print(f"sample limits: CVL={lim['cvl_v']}V CCL={lim['ccl_a']}A DCL={lim['dcl_a']}A "
          f"chg={lim['charge_enable']} dis={lim['discharge_enable']}")
    print("frame round-trip self-test: PASS")

    # 3) SOC aggregation: the reported SOC must be the LEAST-drifted counter.
    agg = Aggregator({"freshness_seconds": 300, "min_fresh_batteries": 1})
    for i, s in enumerate([65, 73, 77, 93, 99, 99, 99, 99]):
        agg.update(f"p{i}", {"voltage": 55.0, "current": 0.0, "soc": s,
                             "cell_max_mv": 3440, "cell_min_mv": 3430,
                             "temp_max": 28.0, "temp_min": 25.0,
                             "fet_charge": "ON", "fet_discharge": "ON"})
    v = agg.virtual()
    assert v["soc"] == 99 and v["soc_mean"] == 88 and v["soc_min"] == 65, v
    print("SOC aggregation self-test: PASS (soc=max=99, mean=88, min=65)")

    # 4) Recal state machine: start -> success -> cooldown -> abort -> timeout.
    import tempfile
    spath = os.path.join(tempfile.gettempdir(), "recal-selftest.json")
    if os.path.exists(spath):
        os.unlink(spath)
    er = dict(e, recal_enable=True, recal_state_path=spath)
    top = dict(v, voltage=55.0, current=0.0)
    rc = RecalController(er)
    assert rc.evaluate(top) == er.get("recal_cvl_volts", 56.8), "should start"
    assert rc.evaluate(dict(top, soc_min=99)) is None and not rc.active, "should succeed"
    rc2 = RecalController(er)                      # state persisted -> cooldown blocks
    assert rc2.evaluate(top) is None and not rc2.active, "cooldown should block"
    os.unlink(spath)
    rc3 = RecalController(er)
    assert rc3.evaluate(top) is not None, "should start again"
    assert rc3.evaluate(dict(top, cell_max_mv=3590)) is None and not rc3.active, "should abort"
    os.unlink(spath)
    rc4 = RecalController(er)
    assert rc4.evaluate(top) is not None
    rc4.active_since -= er.get("recal_timeout_minutes", 180) * 60 + 1
    assert rc4.evaluate(top) is None and not rc4.active, "should time out"
    assert rc4.evaluate(dict(top, voltage=54.0)) is None, "low volts must not start"
    os.unlink(spath)
    rc5 = RecalController(er)
    assert rc5.evaluate(dict(top, current=-8.0)) is None, "discharging must not start"
    assert RecalController(dict(er, recal_enable=False)).evaluate(top) is None
    print("recal state-machine self-test: PASS (start/success/cooldown/abort/timeout)")

    # 5) During a hold: CID63 carries the raised CVL + the full-charge-request
    #    bit (0x08), so an inverter latched at "battery full" re-enters charging.
    lim_r = compute_limits(va, e, 56.8)
    assert lim_r["cvl_v"] == 56.8 and lim_r["full_charge_request"]
    assert int(info_63(lim_r)[-2:], 16) & 0x08, info_63(lim_r)
    lim_n = compute_limits(va, e)
    assert not lim_n["full_charge_request"]
    assert not int(info_63(lim_n)[-2:], 16) & 0x08, info_63(lim_n)
    assert not lim_r["force_charge"], "force bit must stay clear unless opted in"
    lim_f = compute_limits(va, dict(e, recal_force_charge=True), 56.8)
    assert lim_f["force_charge"] and int(info_63(lim_f)[-2:], 16) & 0x20, info_63(lim_f)
    assert not compute_limits(va, dict(e, recal_force_charge=True))["force_charge"], \
        "force bit must clear once the hold ends"
    print("full-charge-request self-test: PASS (bits set during hold, clear otherwise)")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    cfg = json.load(open(CONFIG_PATH))
    if args.selftest:
        selftest()
    else:
        run(cfg)


if __name__ == "__main__":
    main()
