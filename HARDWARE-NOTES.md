# Hardware notes — Phomemo D30

Everything here is reverse-engineered. Read this before changing anything in
`labelfab/device/`.

## Status

**Nothing below marked "unverified" has been tested on a physical printer yet.** The
protocol constants come from [`polskafan/phomemo_d30`](https://github.com/polskafan/phomemo_d30),
which captured them from the Android "Print Master" app. The bring-up checklist at
the bottom exists to turn the unverified rows into verified ones.

## Device

| | |
|---|---|
| Resolution | 203 dpi → **7.9921 px/mm** |
| Print speed | ~60 mm/s (spec) |
| Tape | 6 / 12 / 14 / 15 mm; continuous and die-cut both sold |
| Transport | Bluetooth Classic **SPP** (RFCOMM), *not* BLE GATT — unverified for all firmware revisions |
| Read channel | **None.** The printer never sends anything back. |

`vivier/phomemo-tools` does **not** support this printer. It targets the
M02/M02Pro/M02S/M110/M120/M220/T02 family. Useful only as an ESC/POS raster reference.

## Wire format

One verified byte string exists in the whole project:

```
1f1124001b401d7630000c004001
└─ vendor ─┘└ESC@┘└GS v 0┘└xL xH┘└yL yH┘
   1f112400  1b40  1d763000  0c 00  40 01
                              = 12B  = 320
```

- `1f 11 24 00` — **opaque vendor framing.** `24` looks like an opcode with `00` as
  its argument. Do not "clean this up"; it is empirically required.
- `xL/xH` — bytes per line, little-endian. 12 = 96 px = 12 mm.
- `yL/yH` — **line count, little-endian, plain 16-bit.** Max 65535 = 8.2 m of tape.

### Do not chunk at 255 lines

The reference implementation splits the image into 255-line blocks. That comes from
`theacodes/phomemo_m02s`, written for the M02. The D30 header carries **320** in a
single frame, which already exceeds 255, so the field is clearly a full 16-bit value.
Reintroducing the chunker would emit multiple headers per label and is a plausible
cause of a "prints half the label then feeds" bug. If hardware ever demands it,
`D30Config` gets a `max_lines_per_frame` knob — it does not go back in by default.

### Session setup

Seven packets, each written and flushed **separately** (the printer is fussy about
framing; do not coalesce them):

```
1f1138  1f11121f1113  1f1109  1f1111  1f1119  1f1107  1f110a1f110202
```

Meanings unknown. Probably power/density/mode selection.

## Geometry

| Tape | Pixels | Bytes | `xL/xH` | Verified |
|---|---|---|---|---|
| 12 mm | 96 | 12 | `0c 00` | ✅ (the reference prints this) |
| 15 mm | 120 | 15 | `0f 00` | ❌ **unverified** |

Both are exact multiples of 8, so `Image.tobytes()` on a mode-`"1"` image is already
the wire format with no row padding. This is load-bearing and pinned by a test.

**The 15 mm question.** Phomemo's spec says "printing width 12–15 mm", which reads as
*tape* compatibility, not head width. The head may well be 96 dots, in which case
15 mm tape gets a 12 mm print window that is **not necessarily centred** — the tape
is edge-guided against a fixed head. Until the width sweep says otherwise, treat
`raster_width_px = 96` with a `tape.offset_px` letterbox as the safe default and 120
as a hypothesis.

## Flow control

The printer **streams**; it does not buffer a whole job. A 20-label strip is ~96 KB,
far more than it can hold, so writes are paced to roughly the head's consumption rate:

```
lines_per_chunk = chunk_bytes / width_bytes
delay_s         = lines_per_chunk / (7.9921 * 60) / pace_factor
```

A single 320-line label never hits this. **Buffer overrun only appears on long
strips**, so it will not show up in early testing.

## What cannot be detected

There is no read channel, therefore:

- out of tape — undetectable
- paper jam — undetectable
- low battery — undetectable
- print actually completed — **undetectable**; "printed" means the bytes were accepted
  and we waited the physical print duration

Do not add machine statuses that claim otherwise. `NO_MEDIA` in a UI that can never
be true is worse than no status at all.

## Day-one bring-up checklist

Work through in order. Each step sets one config constant. Record answers here.

### 0. Is it even SPP?

```bash
bluetoothctl            # scan on, find the MAC, pair, trust
sdptool browse <MAC>    # look for a Serial Port Profile record + channel
```

**If there is no SPP record, this unit is BLE-only.** Stop and implement
`device/ble.py` with `bleak` — `crabdancing/phomemo-d30` (Rust) is the reference. The
`Transport` protocol makes that additive rather than a rewrite. Budget half a day.

Record: PIN required? `trust` needed for unattended reconnect? RFCOMM channel?

### 1. Can we open a socket unprivileged?

```bash
labelfab probe --mac <MAC> --self-test --width-px 96 --length-px 200
```

On `EPERM` / `EAFNOSUPPORT`: try as root → add `AmbientCapabilities=CAP_NET_RAW` →
fall back to `rfcomm bind` plus `--transport serial`. Which one worked determines the
systemd unit.

> `RestrictAddressFamilies=` in the unit **must** include `AF_BLUETOOTH`. Every
> hardening cheat-sheet omits it and the failure gives no hint that systemd caused it.

### 2. Alignment — four answers from one print

The self-test is a 1 px border, tick rules every 8 px, and an asymmetric solid block.

- Border complete on all four sides? → effective head width
- Block where expected? → rotation and mirror (`tape.rotation`, `tape.mirror`)
- Border touching both tape edges? → `tape.offset_px`

### 3. Width sweep — settles the 15 mm question

```bash
labelfab probe --mac <MAC> --width-sweep 96,104,112,120   # on 15mm tape
```

Find where it garbles or errors. If 120 fails, letterboxing onto 15 mm is permanent.

### 4. Length sweep — **load-bearing for the whole strip design**

```bash
labelfab probe --mac <MAC> --length-sweep 320,1600,3200,6400
```

Does `yL/yH` really accept a full strip, or does the firmware cap it? If capped at
some N, `strip.max_length_mm` becomes that cap rather than a preference and the tape
economics change. Also: does the printer auto-align to a die-cut gap?

### 5. Waste measurement

Print 10 labels as one strip, then 10 discretely. Measure the tape each consumed.
That quantifies the leader/trailer and sets `separator_mm`.

### 6. Pace sweep

Walk `pace_factor` down until a long strip garbles, then add 50 %.

### 7. Sleep behaviour

Idle 5 / 10 / 15 minutes, then print. Does the socket survive? Does reconnect plus
re-init suffice? Is the first label after wake faint (`device.wake_dummy_feed`)?

### 8. Faintness

If prints are light, increase `inter_packet_delay_s` / lower `pace_factor` **before**
hunting for a density opcode. On these printers, feeding the head slower usually
matters more than any documented density command.

## Results

*(fill in on arrival)*

| Constant | Default | Measured |
|---|---|---|
| `device.transport` | `afbluetooth` | |
| `device.channel` | 1 | |
| `device.raster_width_px` | 96 | |
| `tape.offset_px` | 0 | |
| `tape.rotation` | 270 | |
| `tape.mirror` | false | |
| `device.pace_factor` | 1.2 | |
| `strip.max_length_mm` | 300 | |
| `separator_mm` | 2.0 | |
| `device.wake_dummy_feed` | false | |
