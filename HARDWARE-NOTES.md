# Hardware notes — Phomemo D30

Everything here is reverse-engineered. Read this before changing anything in
`labelfab/device/`.

## Status

The protocol was **validated against the real unit on 2026-07-29**, twice: once
capturing the vendor Android app over Classic SPP, and once capturing the vendor iOS
app over BLE GATT. Rows marked ✅ are confirmed by those captures; rows marked ❌
**unverified** still are not.

Sources, in descending order of authority:

1. **Live captures**
   - Android / SPP — `~/Documents/bt-captures/session-2026-07-29_15-12-12/`
     (`btsnoop_hci.log`, unfiltered, 86 SPP frames + matching `logcat.txt`).
     Reproduce with `~/Documents/bt-capture.sh`.
   - iOS / BLE — `~/Documents/d30-ios.pklg` (Apple PacketLogger, 165 ATT frames).
     Reproduce per "Capturing iOS" below.
2. **Vendor APK decompile** — full opcode tables and the D30 print path in
   `~/Documents/quyin-printer-protocol.md`. That document is the opcode reference;
   this one is what the hardware actually did.
3. `polskafan/phomemo_d30` — the original constants. Still correct, now explained.

`vivier/phomemo-tools` does **not** support this printer. It targets the
M02/M02Pro/M02S/M110/M120/M220/T02 family. Useful only as an ESC/POS raster reference.

## This unit

Read live off the printer (`1f1109` / `1f1107` / `1f1108`), identically over both
transports:

| | |
|---|---|
| Serial | **`Q223P4C31420105`** |
| Model | SN prefix `Q223` → **D30** (vendor `DefaultPrinter.json`, 237 records) |
| Consumable DRM | **`paperEncrypt: false`** — third-party tape will feed ✅ |
| Firmware | **2.1.2** |
| MAC | `AA:FD:FD:6B:9F:5F` (public) |
| Class of Device | `0x100680` → major `0x06` Imaging, minor bit `0x80` **Printer** |

## Device

| | |
|---|---|
| Resolution | 203 dpi → **7.9921 px/mm** |
| Print speed | ~60 mm/s (spec) |
| Tape | 6 / 12 / 14 / 15 mm; continuous and die-cut both sold |
| Print width | **96 dots = 12 bytes** ✅ (`D30Printer.MAX_PRINT_WIDTH`, hardcoded, confirmed on the wire) |
| Transport | **Dual-mode.** Classic SPP ✅ *and* BLE GATT ✅ |
| Read channel | **Exists.** Answers every query *and* pushes unsolicited status ✅ |

## Transport

The printer is genuinely dual-mode, and **the command encoding is identical on both** —
proven by capturing the same three print jobs over each. Only the framing differs.

### Why dual-mode (inference, well supported)

iOS gates Bluetooth Classic SPP behind **MFi** licensing — the External Accessory
framework needs an Apple authentication coprocessor, an agreement, and per-unit
royalties. CoreBluetooth imposes none of that for BLE. So the vendor ships Classic for
Android and BLE for iOS on one device.

Supporting evidence in the firmware's own command set: `InsGet.IOS_DATA_TYPE`
(`1F 11 45`) exists in the shared opcode table with **zero call sites in the Android
app** — and was never sent by the iOS app either, so it remains unexplained. Treat the
MFi rationale as a well-supported inference, not an established fact.

### Classic SPP ✅ — what Android uses

RFCOMM, UUID `00001101-0000-1000-8000-00805F9B34FB`. Captured end to end:
`connectSocket type=1 uuid=00001101…` → `bta_dm_acl_up … transport:BT_TRANSPORT_BR_EDR`.
The device is bonded (`link_key_type: 0x4`) and advertises an Imaging/Printer CoD,
which a BLE-only peripheral would not have.

Writes are a raw stream — no framing, length prefix, checksum or app-level chunking.
The RFCOMM layer fragments and credits automatically; the 2,888-byte print write
appeared on air as 662-byte fragments the app never saw.

### BLE GATT ✅ — what iOS uses

Vendor service `0xff00`:

| Handle | UUID | Role |
|---|---|---|
| `0x0006` | `ff01` | read |
| `0x0008` | `ff02` | **write** — all commands and raster |
| `0x000a` | `ff03` | **notify** — all responses |
| `0x000b` | `0x2902` | CCCD for `ff03` |

- **MTU negotiated to 200** ✅ (iOS requested 527, the printer capped it). Not the
  23-byte default — always request a large MTU, it costs nothing.
- The vendor app writes in **182-byte chunks**, ~30 ms apart, using ATT
  `Write Command` (`0x52`, write-without-response).

#### The `0101` ACK — BLE flow control 🔑

Every write to `ff02` is answered by a `0101` notification on `ff03` **before** any
data response:

```
PHONE→PRN  h=0x0008  <182 bytes>
PRN→PHONE  h=0x000a  0101          ← ACK
PHONE→PRN  h=0x0008  <182 bytes>
PRN→PHONE  h=0x000a  0101          ← ACK
```

This has no SPP equivalent — it is the BLE substitute for RFCOMM credits.
**Wait for `0101` instead of sleeping a computed interval.** Feedback-driven pacing
cannot garble the way an open-loop `pace_factor` can, and it removes the whole
`inter_packet_delay_s` guessing game on this transport.

### Which to prefer

**SPP**, where available. RFCOMM is a stream with link-layer flow control; BLE needs
chunking, ACK tracking, and MTU negotiation to reach comparable throughput. But BLE is
fully viable and the ACK makes it safe.

## Wire format

### Command families

| Prefix | Meaning |
|---|---|
| `1F 11` | vendor control plane — get/set device state |
| `1B 4E` | vendor config plane — persistent defaults |
| ESC/POS | `1B 40` init, `1D 76 30 00` = `GS v 0` raster, `1B 64` feed |

Full opcode tables (~50 set, ~45 query) live in `~/Documents/quyin-printer-protocol.md`.

### `1f 11 24 00` is not opaque framing

The original note called this "opaque vendor framing … do not clean this up." It is
**`LEFT_MARGIN` with argument `0`**:

```
InsSet.LEFT_MARGIN = 1F 11 24        QuinPrinter.setMargin(i) -> send(LEFT_MARGIN, [i])
D30Printer:  margin = (width/8) < 12 ? 12 - (width/8) : 0
```

The `00` is correct **only for a full-width 12-byte image**. A narrower label must
recompute it or the image lands hard against one edge. Do not hardcode it.

Note the iOS app omits `LEFT_MARGIN` entirely and still prints — see below.

### Verified print sequence ✅

Captured three times per platform, rasters byte-identical within each platform, only
the density byte differing.

```
Android:  1f110a  1f1102 <d>  |  1f1124 00  1b40                | GS v 0 …
iOS:      1f1102 <d>  1f110a  |  1f1121 01  1b40  1f1135 00     | GS v 0 …
```

Common to both: `PRINT_DENSITY`, the unidentified `1f110a`, and `1b40` (`ESC @`).
iOS additionally sends `PRINT_MULTI = 1` (`1f1121 01`) and `EXIT_COMPRESS_MODE`
(`1f1135 00`); Android additionally sends `LEFT_MARGIN` (`1f1124 00`).

**The preamble is tolerant** — both variants print correctly. Recommended for
`labelfab`: send density, `1f110a`, `LEFT_MARGIN <computed>`, `1f1135 00`, `1b40`,
then the raster. `1f1135 00` explicitly leaves compress mode and is cheap insurance.

Then the raster frame itself:

```
1d 76 30 00 xL xH yL yH     ; GS v 0
<widthBytes * height bytes> ; raster
```

Live example on **both** platforms: `1d7630000c00f000` = 12 bytes (96 dots) × 240
lines, followed by exactly 2880 bytes. 12 × 240 = 2880 ✅.

- `xL/xH` — bytes per line, little-endian.
- `yL/yH` — line count, little-endian, **plain 16-bit** ✅ (0x00f0 = 240 in one frame).
- Density: **1 = light, 2 = medium, 4 = heavy** ✅ (matches `D30Constant.TYPE_CONCENTRATION_*`).
- `1f110a` — unidentified. Once per job, adjacent to density, on both platforms. Send it.

### Do not chunk at 255 lines

Confirmed correct. The header carried 240 in a single frame on both transports, and
`XNvUtil` writes `buf[2] = height % 256; buf[3] = height / 256` with no block logic
anywhere. The 662-byte (SPP) and 182-byte (BLE) fragments are **transport MTU**, not
protocol structure. Reintroducing an app-level chunker would emit multiple headers per
label. It does not go back in.

### Raster encoding ✅

`XNvUtil.img2Nv(bmp, threshold=128, rotate180=true)`:

- threshold at 128, **1 bit = dark**, MSB-first
- `widthBytes = width / 8` — integer division, **remainder columns are silently dropped**
- D30 always passes `rotate180=true`: rows iterate `height-1 → 0`, columns `width-1 → 0`
- the 4-byte `xL xH yL yH` header is produced by the encoder itself and sits directly
  after `1d 76 30 00`

Byte-identical to a P4 PBM body, which makes verification trivial:
`open(f,'wb').write(b"P4\n96 240\n" + raster)`.

**Cross-platform raster check.** The same sticker printed from Android and iOS gave
`sha=c9542aea…` vs `sha=5b637f69…` — different bytes, identical dimensions, ink 3535 vs
3532 pixels, and visually the same layout. The delta is **font rasterisation at the
threshold**, not encoding. Framing and geometry are identical across platforms.

### Session setup — decoded ✅

Both apps open with the same query set. Android batches several into one write
(`1f11381f11121f11131f1107`); iOS sends each individually.

| Bytes | Meaning |
|---|---|
| `1f1138` | `CHIP_TYPE` |
| `1f1112` | `COVER_STATE` |
| `1f1113` | `HOT_STATE` |
| `1f1109` | `SN` — drives model identification |
| `1f1111` | `PAPER_STATE` |
| `1f1119` | `LABEL_TYPE` |
| `1f1107` | `FIRMWARE_VERSION` |
| `1f1108` | `BATTERY` |
| `1f110e` | `AUTO_POWER_TIME` |
| `1f1165` | `POWER_KEY_TYPE` (iOS only) |
| `1f114a` | `BT_LOSS_TEST` / `GET_DATE_TITLE` — opcode collision, iOS only |
| `1f110a` | unidentified (also appears per print job) |
| `1f110202` | `PRINT_DENSITY = 2` — the only *set* in the group |

## Responses — the printer talks back ✅

Framing is `1A <tag> <payload>`, identical on both transports. Payload length is
tag-dependent. Response tags are their own namespace and do **not** equal the request
subcode.

| Query | Response | Decoded |
|---|---|---|
| `1f1107` FIRMWARE_VERSION | `1a07 02 01 02` | v2.1.2 |
| `1f1109` SN | `1a08 <15 ASCII>` | `Q223P4C31420105` |
| `1f1108` BATTERY | `1a04 58` | 88 % |
| `1f1111` PAPER_STATE | `1a06 89` | bitfield, see below |
| `1f1119` LABEL_TYPE | `1a0c 0a` | |
| `1f110e` AUTO_POWER_TIME | `1a09 <n>` | echoes the set value |
| `1f1138` CHIP_TYPE | `1a17 03` | |
| `1f1112` COVER_STATE | `1a05 98` | |
| `1f1113` HOT_STATE | `1a03 a8` | |
| *(after a print)* | `1a0f 0c` | **print complete**, ~2.4 s after the raster |

On BLE these arrive as `ff03` notifications, interleaved with the `0101` write-ACKs.
Do not confuse the two: `0101` is transport-level, `1a…` is protocol-level.

### Full response surface — 29 branches

The vendor parser handles 29 tags; we have observed 11. Full table with payload
lengths in `~/Documents/quyin-printer-protocol.md` §3.4. The ones not yet observed
but worth having:

| Tag | Len | Meaning |
|---|---|---|
| `0x3B` | 4 | **capability bits** — multi-concentration, multi-connection, UID support, red/black. Feature detection instead of assumption |
| `0x40` | 15 | RFID media descriptor — material no, colours, lamination, paper type, **length × width** |
| `0x15` | 3 | consumable remaining (ribbon / RFID / carbon) |
| `0x3E` | 1 | print busy |
| `0x3F` | 1 | consumable / material error |
| `0x31` | 3 | RFID consumable number |
| `0x0B` | 1 | print cancelled |
| `0x35` | 1 | charging state |
| `0x99` | var | consumable UID, length-prefixed |

`0x3B` is the most valuable unobserved tag — it is the `InsProcessor` D0/D1/D2 bitmask
and would let us feature-detect compression, dual-colour and UID support rather than
guessing.

### ⚠ Do not copy the vendor parser

`QuinPrinter.InstructionProcessor.process()` is a byte-scanner, not length-aware, and
**desyncs silently on at least two tags**:

- `case 22` (`0x16`) consumes the tag and **no payload**, but `VERIFY_PAPER` answers
  `1a16 00 40 00 00`. The remainder falls into the `0x99` handler, which reads `0x40`
  as a length, attempts a 64-byte copy from a 6-byte buffer, throws
  `ArrayIndexOutOfBoundsException` — and the caller catches it and discards the rest of
  the buffer.
- `case 9` (`0x09`) advances the cursor **only if a callback is registered**, so with no
  listener the payload byte is reparsed as a tag.

Unknown tags are not skipped; they fall into the length-prefixed UID reader. Our parser
must be table-driven on payload length, with skip-and-warn on unknown tags.

Those four bytes after `1a16` are undocumented measurement data the vendor **discards
entirely** — likely the paper-detection result. Worth capturing across media types.

## Coverage

| | Count | |
|---|---|---|
| Opcodes in vendor tables | **102** | superset, all ~70 models |
| Reachable on the D30 path | **47** | via `QuinPrinter`; D30 overrides only the 5 print methods |
| **Observed working on hardware** | **21** | across SPP + both BLE captures |
| Implemented in `labelfab` | see `protocol.py` | each carries a verification status |
| Response branches in the APK | **29** | 11 observed |

**Never sent by any model (55 opcodes)** — declared in the vendor tables, called from
nowhere in the app. Firmware surface with no UI. The useful ones:

| Command | Bytes | Why |
|---|---|---|
| `LABEL_WIDTH` get / set | `1F 11 18` / `17` | 🎯 **would settle the 15 mm question directly** — ask the printer its own width |
| `PRINT_TEST_PAGE` | `1F 11 27` | built-in self-test, no rasteriser needed |
| `ALL_ERROR` | `1F 11 28` | comprehensive error word; would decode the `PAPER_STATE` bits we lack |
| `FEED_PAPER` | `1F 11 32` | feed without printing |
| `PRINT_AND_FEED` | `1B 64 n` | the feed used by the continuous path |
| `HEART_BEAT` | `1A 18 01` | the keep-alive question, still unresolved |
| `PAPER_LEARN` / `AUTO_LOCATE` | `1F 11 1E` / `25` | gap detection for die-cut |
| `SENSOR_INFO` / `SENSOR_HEAT` / `VOLTAGE` | `1F 11 1D` / `3A` / `1F` | raw diagnostics |
| `HARDWARE_VERSION` / `COMM_VERSION` | `1F 11 33` / `34` | protocol version for feature gating |
| `COMPRESS_TYPE` / `COMPRESS_SIZE` | `1F 11 51` / `36` | whether minilzo is supported |
| `SHUTDOWN` | `1F 11 42` | remote power off |

### ☠ Never send these

- **`OTA_MODE` `1F 11 0F`** and every `FIRMWARE_UPGRADE_*` — brick risk, no recovery.
- **`DEVICE_ID` `1B 4E 08`** — rewrites the serial. The `Q223` prefix is what identifies
  this as a D30; corrupt it and model resolution breaks permanently.
- **`SET_CRIMP_MODE`, `MATERIAL_CONFIG`, `TOUCH_ENCRYPT`, `BT_ENCRYPT`** — unknown
  semantics, plausibly persistent.

### Unsolicited notifications ✅

The printer pushes state changes with no preceding query. Captured on SPP during a
deliberately induced stripe error:

```
t=105.80  1a0f0c   print complete
t=116.34  1a0688   UNSOLICITED  paper state 0x89 -> 0x88   (error induced)
t=126.33  1a0689   UNSOLICITED  paper state 0x88 -> 0x89   (error cleared)
```

`0x89 = 1000_1001`, `0x88 = 1000_1000` → **bit 0 is paper-OK**.

## What CAN be detected

The original note said none of it was possible. It is:

| Condition | Mechanism | Status |
|---|---|---|
| Media error / stripe | `1a06` bit 0, pushed async | ✅ observed |
| Print completed | `1a0f 0c` after the raster | ✅ observed |
| Battery level | `1f1108` → `1a04 <pct>` | ✅ observed |
| Cover state | `1f1112` → `1a05 <v>` | ✅ answers; encoding unmapped |
| Head over-temp | `1f1113` → `1a03 <v>` | ✅ answers; encoding unmapped |
| All errors | `1f1128` (`ALL_ERROR`) | ❌ never exercised |

`labelfab` should therefore expose real machine statuses and a genuine "printed"
signal rather than a timer. The honest remaining gap: only bit 0 of `PAPER_STATE` is
decoded — other bits and the cover/heat encodings need a capture per fault type.

## Geometry

| Tape | Pixels | Bytes | `xL/xH` | Verified |
|---|---|---|---|---|
| 12 mm | 96 | 12 | `0c 00` | ✅ captured live on both transports, 96 × 240 |
| 15 mm | 120 | 15 | `0f 00` | ❌ **unverified** |

Both are exact multiples of 8, so `Image.tobytes()` on a mode-`"1"` image is already
the wire format with no row padding. This is load-bearing and pinned by a test.

**The 15 mm question — now leaning strongly to 96.** `D30Printer.MAX_PRINT_WIDTH = 12`
is a hardcoded static, never reassigned anywhere in the vendor app (compare M420 = 114,
M330 = 72, E6000 = 47), and the app letterboxes narrower images via `LEFT_MARGIN`
rather than widening. Treat `raster_width_px = 96` with a `tape.offset_px` letterbox as
correct until the width sweep proves otherwise; 120 is now a weak hypothesis.

## Flow control

**SPP** — none needed. RFCOMM fragments and credits automatically. A single 240-line
label needs no pacing at all.

**BLE** — use the `0101` ACK, not a timer. Write a chunk, wait for the notification,
write the next. The vendor uses 182-byte chunks at MTU 200 with ~30 ms spacing, but
that spacing is a consequence of the ACK round-trip, not a configured delay.

The open-loop model is retained only as a fallback for a device that turns out not to
ACK:

```
lines_per_chunk = chunk_bytes / width_bytes
delay_s         = lines_per_chunk / (7.9921 * 60) / pace_factor
```

**Buffer overrun only appears on long strips**, so it will not show up in early testing.

## Day-one bring-up checklist

### 0. Transport — ✅ RESOLVED

The unit is **dual-mode**; both transports print, with identical command encoding.
SPP preferred, BLE fully viable.

BLE details: service `0xff00`, write `0000ff02`, notify `0000ff03`, CCCD `0x000b`;
negotiate MTU (200 granted); `trust` needed for unattended reconnect; the printer must
be awake and not held by another BlueZ connection.

> **OPEN QUESTION — pairing and wake behaviour.** Connecting is unreliable in a way
> that is not yet characterised. On SPP the app's first connect failed with
> `channel=-1` (SDP resolved nothing) and succeeded ~25 s later. On BLE, pairing to the
> iPhone required **restarting the printer twice**. The earlier `sdptool browse`
> returning empty is probably the same phenomenon. This needs deliberate investigation
> — how long the printer advertises after wake, whether bonding state is the trigger,
> whether an existing connection from another host blocks it — **before** deciding
> whether the answer is a retry loop, a wake sequence, or something else. Do not
> paper over it with blind retries until it is understood.

### 1. Can we open a socket unprivileged?

```bash
labelfab probe --mac AA:FD:FD:6B:9F:5F --self-test --width-px 96 --length-px 240
```

On `EPERM` / `EAFNOSUPPORT`: try as root → add `AmbientCapabilities=CAP_NET_RAW` →
fall back to `rfcomm bind` plus `--transport serial`.

> `RestrictAddressFamilies=` in the unit **must** include `AF_BLUETOOTH`. Every
> hardening cheat-sheet omits it and the failure gives no hint that systemd caused it.

### 2. Alignment — four answers from one print

The self-test is a 1 px border, tick rules every 8 px, and an asymmetric solid block.

- Border complete on all four sides? → effective head width
- Block where expected? → rotation and mirror (`tape.rotation`, `tape.mirror`)
- Border touching both tape edges? → `tape.offset_px`

Note the vendor rotates 180° **inside the raster encoder**, a different axis from
`tape.rotation`. Do not assume they cancel.

### 3. Width sweep — settles the 15 mm question

```bash
labelfab probe --mac <MAC> --width-sweep 96,104,112,120   # on 15mm tape
```

### 4. Length sweep — **load-bearing for the whole strip design**

```bash
labelfab probe --mac <MAC> --length-sweep 320,1600,3200,6400
```

240 lines is confirmed working in one frame; the field is 16-bit. Unknown whether
firmware caps it below 65535. Also: does the printer auto-align to a die-cut gap?

### 5. Waste measurement

Print 10 labels as one strip, then 10 discretely. Measure the tape each consumed.

**Open question:** both captured jobs used the die-cut path — no feed after the raster.
The vendor's *continuous* path (`printConstinuous`) instead sends `1b 40` once and then
`1d 76 30 00 …` + **`1b 64 17`** (feed 23 lines ≈ 2.88 mm) after each label. Default
media here is continuous, so **that feed is still unverified** and 23 lines is the
vendor's own separator value — compare against `separator_mm = 2.0`.

### 6. Pace sweep

Only meaningful on BLE, and probably unnecessary — use the `0101` ACK instead. Over
SPP, start with pacing disabled.

### 7. Sleep behaviour

Idle 5 / 10 / 15 minutes, then print. Does the socket survive? Does reconnect plus
re-init suffice? Is the first label after wake faint (`device.wake_dummy_feed`)?
Overlaps with the pairing open question in step 0.

`AUTO_POWER_TIME` is settable — `1b 4e 07 <n>`, **units of 5 minutes**, `0x00` = off ✅.
Confirmed across both platforms:

| UI selection | byte | minutes |
|---|---|---|
| off | `0x00` | — |
| 10 m | `0x02` | 2 × 5 = 10 |
| 30 m | `0x06` | 6 × 5 = 30 |
| 60 m | `0x0c` | 12 × 5 = 60 |
| 10 h | `0x78` | 120 × 5 = 600 |

Setting it to `0x00` during a print session sidesteps sleep entirely.

### 8. Faintness

Density is `1f 11 02 <n>`, **n = 1 light / 2 medium / 4 heavy** ✅. Try that first.
A much deeper thermal surface exists if it is ever needed —
`PRINT_TEN_CONCENTRATION_PARAMS` (`1B 4E 62 14`) carries `baseHeatZeroLayer`,
`compensationRatio*`, `stbOpenPercentage`, `stbClosePercentage`, `maxSpeed`,
`concentrationRatio` (see `TenConcentrationParams` in the APK). Untested.

## Results

| Constant | Default | Measured |
|---|---|---|
| `device.transport` | `afbluetooth` | **SPP ✅ and BLE ✅ both work**, identical encoding |
| `device.channel` | 1 | — (SDP-resolved; app used UUID lookup, not a fixed channel) |
| `device.raster_width_px` | 96 | **96 ✅** |
| `tape.offset_px` | 0 | — |
| `tape.rotation` | 270 | — (vendor rotates 180° inside the encoder; different axis) |
| `tape.mirror` | false | — |
| `device.pace_factor` | 1.2 | n/a on SPP; on BLE use the `0101` ACK instead |
| `device.ble_mtu` | — | **200 granted** (527 requested); vendor chunks at 182 B |
| `strip.max_length_mm` | 300 | — (240 lines ≈ 30 mm proven; 16-bit field) |
| `separator_mm` | 2.0 | vendor uses `1b 64 17` = 23 lines ≈ **2.88 mm** (continuous path, unverified) |
| `device.wake_dummy_feed` | false | — |
| `device.print_density` | — | **1 / 2 / 4** = light / medium / heavy ✅ |
| `device.auto_power_off` | — | `1b4e07 <n>`, **5-minute units**, `0x00` = off ✅ |

## Reproducing the captures

### Android / SPP

```bash
~/Documents/bt-capture.sh          # arms; triggers on the device's Bluetooth toggle
```

Requires root adbd, `persist.bluetooth.btsnooplogmode=full`, and
`persist.bluetooth.snooplogfilter.profiles.rfcomm.enabled=false` — **without that last
one every RFCOMM payload is silently stripped** and the capture is worthless. Decode:

```bash
tshark -r btsnoop_hci.log -Y btspp \
       -T fields -e frame.number -e frame.p2p_dir -e btspp.data
```

Two traps: `settings get global bluetooth_on` lags the real toggle by ~16 s on this
phone (the script polls `dumpsys bluetooth_manager` instead), and Android wireless-debug
ports rotate constantly — rediscover with `nmap -p- --open -Pn <host>`.

### iOS / BLE

1. Install the **Bluetooth logging profile** (developer.apple.com → Profiles and Logs)
   on the iPhone, then **reboot** — it does nothing until you do.
2. On a Mac, install *Additional Tools for Xcode* → `Hardware/PacketLogger.app`
   (it is not bundled with Xcode itself).
3. Tether by USB, trust the Mac, unlock the phone, start a trace **before** enabling
   Bluetooth so connection setup is inside the window.
4. Run the vendor app, print, stop, File → Save As `.pklg`.

Wireshark reads Apple PacketLogger natively. Decode:

```bash
tshark -r d30-ios.pklg -Y 'btatt.opcode==0x52 || btatt.opcode==0x12 || btatt.opcode==0x1b' \
       -T fields -e frame.number -e frame.time_relative -e btatt.opcode \
                 -e btatt.handle -e btatt.value
```

`0x52` = write command (phone→printer, handle `0x0008`), `0x1b` = notification
(printer→phone, handle `0x000a`). Reassemble by concatenating all `0x0008` writes and
splitting on the `1d763000` marker.
