# labelfab

Queue-driven label rendering and printing for the **Phomemo D30**, on Linux, with no GUI
and no vendor app.

Publish a JSON job to MQTT; a small daemon on the machine paired with the printer renders
QR codes, barcodes and text, packs them into a single thermal raster, and prints them as
one continuous strip.

```bash
apt install labelfab-agent          # via https://apt.sengine.cloud
labelfab probe --self-test alignment
```

## Why this exists

[`vivier/phomemo-tools`](https://github.com/vivier/phomemo-tools) is the usual answer for
Phomemo printers on Linux, but it **does not support the D30** — it covers the
M02/M110/M120/M220/T02 family. The only working D30 reference is
[`polskafan/phomemo_d30`](https://github.com/polskafan/phomemo_d30), a ~120-line script
that prints one hardcoded label size. There is no maintained D30 library, so this is one.

## What it does differently

**One strip, not N labels.** The print head sits behind the exit slot, so every separate
print job wastes a leader and a trailer feed — easily 50% of a 40mm label. labelfab
buffers jobs for a short window and emits the batch as a *single* `GS v 0` frame, paying
that overhead once instead of once per label. A 20-label batch is one 6400-line raster.

**The agent renders.** Producers send `{"preset": "stock_item", "vars": {...}}`, not a
bitmap. Layout lives in one place, works offline, and changing where the QR sits does not
mean redeploying whatever produced the job.

**It refuses to print garbage.** A QR that would come out below 2 device pixels per module
raises `QrTooDense` instead of emitting an unscannable label. Same for barcodes below the
0.25mm narrow-bar floor.

## Keep QR payloads short

At 203dpi a 15mm label is 120 pixels across, so a QR gets 2–3 pixels per module. Capacity
is not the constraint — *physical module size* is. Measured, decoding the image
reconstructed from the actual wire bytes:

| Payload | 12mm, 1:1 | 12mm, degraded | 15mm, 1:1 | 15mm, degraded |
|---|:--:|:--:|:--:|:--:|
| `SI4821` (6 ch) | ✅ | ✅ | ✅ | ✅ |
| `https://sngn.top/s/4821` (23 ch) | ✅ | ❌ | ✅ | ✅ |
| `https://parts.sengine.cloud/stock/item/4821/` (44 ch) | ✅ | ❌ | ✅ | ❌ |

"Degraded" is a half-resolution round trip, standing in for a phone camera at arm's length.
Everything decodes under ideal conditions; the difference only shows up in the case you
actually care about. Set `render.qr_base_url` to a short redirector and encode a token, not
a full URL. The table is pinned in `tests/test_scannability.py`, so if layout changes the
test fails and this table gets revisited.

## Layout

| Package | What |
|---|---|
| `labelfab.contract` | Pydantic job schema; the wire contract, versioned and CI-diffed |
| `labelfab.render` | Layout solver, QR/barcode/text elements, 1-bit rasteriser |
| `labelfab.device` | ESC/POS framing, Bluetooth transport, `PhomemoD30` |
| `labelfab.agent` | MQTT source, SQLite spool, strip coalescer, print worker |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                      # no hardware required
pytest -m "not zbar"        # if libzbar0 is missing
ruff check src tests
```

Everything except `-m hardware` runs against a fake transport that captures the bytes the
printer would have received. `labelfab decode` turns those bytes back into a PNG, which is
how the wire format is verified without a printer.

## Hardware notes

Protocol constants, tape geometry and the day-one bring-up checklist live in
[`HARDWARE-NOTES.md`](HARDWARE-NOTES.md). Read it before changing anything in
`labelfab.device`.

**There is a read channel**, contrary to the received wisdom: the D30 answers queries on
both transports and pushes media-state changes unsolicited. The retained status topic
carries what it said — serial, firmware, battery percentage and terminal voltage, and
whether media is loaded — not just whether the agent is up.

It says so *and when it said so*. The printer is only reachable while it is being printed
to (it auto-powers-off, and a held-open socket just relocates that), so the agent
remembers the last thing it heard and republishes it, stamped with `device_seen_at`.
A consumer can then tell live truth from remembered truth instead of guessing, and no
printer is woken to keep a status page tidy. `device.probe_on_start` surveys the printer
once at startup when it happens to be awake.

Jams remain undetectable — the media bit distinguishes loaded from not, and nothing
observed so far distinguishes a jam from either.

## License

MIT. Bundled DejaVu fonts are under their own permissive license, see
`src/labelfab/render/_fonts/LICENSE`. The raster packing approach derives from
[`theacodes/phomemo_m02s`](https://github.com/theacodes/phomemo_m02s) (MIT).
