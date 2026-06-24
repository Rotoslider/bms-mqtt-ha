# Closed loop (Goal 3): feed the packs back to the inverter

This is the optional second stage. The bridge (`bms_mqtt.py`) reads every pack
over Bluetooth and publishes it to MQTT. The emulator (`pylon_emulator.py`)
reads that MQTT data, boils all the packs down to **one virtual battery**, and
answers the inverter on RS485 **as if it were a Pylontech**. The inverter then
charges and discharges from real cell data — throttling on the *weakest* cell —
instead of guessing state of charge from terminal voltage.

> ⚠️ **This stage talks to your inverter and can change how it charges an
> expensive battery bank.** Go through it slowly, use the dry-run first, and keep
> the one-line revert (below) within reach. Every threshold in the `emulator`
> config must be set to **your** battery's datasheet, not copied blindly.

The tested rig: Raspberry Pi 4 (already running Solar Assistant) → second
USB-to-RS485 adapter → **Sungold SPH5048P** (an SRNE board) on protocol **PYL**,
driving 8× Vatrer 51.2 V 100 Ah packs. The protocol is the Pylontech "6x branch"
(CID2 `0x61` analog / `0x62` alarm / `0x63` charge-discharge management) that
SRNE/Sungold/Growatt-class inverters speak.

---

## 1. What the emulator actually sends

Each poll, it aggregates the fresh packs into one virtual battery:

| Field | How it's combined |
|---|---|
| Pack voltage | **average** of all packs (they're in parallel ≈ same voltage) |
| Pack current | **sum** of all packs (total bank current) |
| State of charge | **average** of the packs' BMS SOC |
| Highest / lowest cell | the **worst** cell across *every* pack |
| Highest / lowest temp | the **worst** temperature across every pack |

From the worst cell + worst temp it derives the four numbers the inverter cares about:

- **CVL** – charge voltage limit (target, e.g. 55.2 V)
- **DVL** – discharge voltage limit (floor, e.g. 48.0 V)
- **CCL** – charge current limit — **tapers down** as the highest cell rises past
  `cell_chg_taper_start_mv`, hits the floor near `..._end_mv`, and charging is
  **disabled** at `cell_overvolt_mv` or outside the charge temperature window
- **DCL** – discharge current limit — same idea against the lowest cell, disabled
  at `cell_undervolt_mv` or outside the discharge temperature window

The whole point: the inverter throttles on the single weakest cell, so strong
cells never get shoved while a weak one catches up.

---

## 2. Fail-safe behavior (read this)

- **Stale data → silence.** If fewer than `min_fresh_batteries` packs have data
  newer than `freshness_seconds`, the emulator **stops answering**. The inverter
  raises a BMS-comms fault and falls back to its own voltage-based settings —
  it does **not** act on stale numbers.
- **Read-only to the batteries.** The emulator only ever *writes* to the
  inverter's RS485 port. It never sends anything to a BMS; pack data comes one
  way, over MQTT.
- **One-line revert.** Set the inverter's RS485-2 port function back to `485`
  (plain monitor mode) — or just stop the service — and you are back to exactly
  how the system ran before. Nothing on the battery is changed.

---

## 3. Hardware

You need a **second** USB-to-RS485 adapter (the first one is your monitor link,
e.g. Solar Assistant). Wire its A/B pair to the inverter's **battery-comms**
RS485 port.

- On the tested Sungold SPH the BMS port is an **RJ45**; A/B land on **pins 7 & 8**.
  *Confirm your own inverter's RS485 pinout from its manual* — vendors differ, and
  a CAN-only port will not work (this Sungold is RS485-only).
- RS485 is half-duplex: that single A/B pair carries both directions. The battery
  is the slave; the inverter is the master that polls.
- 9600 8N1 on the tested inverter.

---

## 4. Stable device names (udev) — do this before anything else

With two identical FTDI adapters, the kernel can swap `/dev/ttyUSB0` and
`ttyUSB1` across reboots. Pin them by serial so the emulator and your monitor
never get crossed:

```bash
# find each adapter's serial (plug them in one at a time to be sure which is which)
for d in /dev/ttyUSB*; do
  echo "$d -> $(udevadm info -q property -n "$d" | grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT')"
done

# edit the example with YOUR two serials, then install it
cp udev/99-rs485-adapters.rules.example /etc/udev/rules.d/99-rs485-adapters.rules
sudoedit /etc/udev/rules.d/99-rs485-adapters.rules     # replace the REPLACE-* serials
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/rs485-*        # rs485-inverter and rs485-bms should both appear
```

The emulator config points at `/dev/rs485-bms`. The monitor adapter
(`/dev/rs485-inverter`) is left alone — **don't** point Solar Assistant at the
emulator's adapter (see §8).

---

## 5. Configure the emulator

The `emulator` block in `config.json` (see `config.example.json`):

| Key | Meaning |
|---|---|
| `serial_port` / `baud` | the BMS adapter (`/dev/rs485-bms`) and inverter baud (9600) |
| `listen_only` | **`true` = dry run**: log what it *would* send, transmit nothing |
| `freshness_seconds` | how old pack data may be before it's "stale" (fail-safe) |
| `min_fresh_batteries` | how many packs must be fresh to keep answering |
| `cells` | cells per pack (16 for these 51.2 V packs) |
| `cvl_volts` / `dvl_volts` | charge target / discharge floor for the bank |
| `ccl_max_amps` / `dcl_max_amps` | max charge / discharge current you allow |
| `ccl_min_amps` / `dcl_min_amps` | the floor the taper eases down to |
| `cell_chg_taper_start_mv` / `_end_mv` | begin / finish easing charge current |
| `cell_overvolt_mv` | highest-cell mV that **stops** charging |
| `cell_dis_taper_start_mv` / `_end_mv` | begin / finish easing discharge current |
| `cell_undervolt_mv` | lowest-cell mV that **stops** discharging |
| `charge_temp_min_c` / `_max_c` | temperature window charging is allowed in |
| `discharge_temp_min_c` / `_max_c` | temperature window discharging is allowed in |
| `force_charge_soc` | SOC under which to request a charge regardless |

> The shipped values are the tested **Vatrer** numbers: charge stops at
> 3.60 V/cell and tapers 3.48→3.55 V — comfortably below that pack's 3.65 V
> charge limit and 3.75 V BMS hard trip, with charge current capped at the
> inverter's 50 A. **Re-derive all of these from your own datasheet.** Conservative
> beats clever when the bank is expensive and irreplaceable.

---

## 6. Test on the bench — no wires to the inverter yet

```bash
# codec + sample frames, no serial port touched:
python3 pylon_emulator.py --selftest
```

You should see `codec self-test: PASS`, three sample frames marked `valid=True`,
and a sample limits line. If that passes, the frame math matches the spec.

---

## 7. Dry run against the live inverter (`listen_only: true`)

Set the inverter's RS485-2 port to BMS / PYL but keep `listen_only: true`:

- **Sungold SPH menu** (yours may differ): setting **[32] = BMS** (port function:
  make the inverter the master that polls the battery) and **[33] = PYL**
  (Pylontech protocol). On the tested unit, leaving these on the *separate* BMS
  port did **not** disturb the Solar Assistant monitor link on the other port —
  but verify that live on your inverter.
- Run the emulator and watch it log the inverter's polls **without transmitting**:

```bash
BMS_CONFIG=/opt/bms-mqtt/config.json python3 pylon_emulator.py
# look for "LISTEN-ONLY" + the inverter's 0x61/0x62/0x63 polls arriving
```

If you see the inverter polling, your wiring, baud and pinout are right.

---

## 8. Solar Assistant users — keep the two ports separate

- Leave Solar Assistant's **battery source on "use inverter values."** Do **not**
  add the emulator's adapter as a USB battery in the SA UI — both SA and the
  emulator driving the same port collides and breaks the loop.
- SA keeps using *its own* adapter (`/dev/rs485-inverter`) for monitoring. The
  closed loop lives entirely on the second adapter.
- Don't change the inverter's RS485 *address* setting used by your monitor port.

---

## 9. Go live

1. Set `"listen_only": false` in `config.json`.
2. Install and start the service:

```bash
sudo cp systemd/bms-emulator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start bms-emulator
journalctl -u bms-emulator -f      # watch it answer every poll
```

3. Confirm the inverter now shows the battery as **BMS-connected** and that
   charge/discharge limits track your config. Watch for a few minutes: every
   inverter poll should get a reply, with no gaps.
4. Once you're happy it survives a reboot the way you want:

```bash
sudo systemctl enable bms-emulator
```

> Pick a moment for go-live when a brief power blip is acceptable. If the inverter
> ever sees no valid reply right after you switch it to BMS mode, it can fault and
> stop charge/discharge until it gets data (or you revert).

---

## 10. Revert (any time)

- **Fastest:** on the inverter, set the RS485-2 port function back to `485`. The
  inverter ignores the emulator and runs on its own voltage settings.
- **Or:** `sudo systemctl stop bms-emulator` (and `disable` it). Stale-data
  fail-safe means the inverter falls back on its own anyway.
- Nothing on the batteries is touched by either path — the emulator never writes
  to a BMS.

---

## 11. State of charge & the shunt question

SOC now comes from the packs' BMS coulomb counters (averaged), which is a big
step up from voltage guessing — but those counters only stay honest if the bank
charges **all the way full** now and then to re-zero. If you ever see SOC drift,
add a bank-level shunt (e.g. a Victron SmartShunt) for one authoritative number.
For a balanced bank that regularly hits full, the BMS average is usually plenty.

---

## Credits

Frame layout verified against `Fahmula/esphome-pylontech-rs485` and real SRNE
polls. Pylontech LV "6x branch": ASCII-hex frames `~`+VER+ADR+CID1CID2+LENGTH+
INFO+CHKSUM+CR; checksum is the two's-complement of the ASCII byte sum; temps are
`(°C + 273.15) × 10` deci-kelvin; `0x61` current ×100 (centi-A), `0x63` currents
×10 (deci-A). See `pylon_emulator.py` for the exact codec and `--selftest`.
