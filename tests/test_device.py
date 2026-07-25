import pytest
from PIL import Image

from labelfab.contract import LabelSpec, QrElement, TapeSpec, TextElement, mm_to_px
from labelfab.device import (
    INIT_PACKETS,
    D30Config,
    D30ConnectError,
    D30GeometryError,
    D30NotReady,
    DecodeError,
    FakeTransport,
    PhomemoD30,
    decode,
    decode_frames,
    find_frames,
    print_header,
)
from labelfab.device.d30 import LINES_PER_SECOND
from labelfab.render import concat_strip, render_label, to_device
from labelfab.render.raster import DeviceRaster

TAPE_15 = TapeSpec(width_mm=15)


def _printer(**cfg) -> tuple[PhomemoD30, FakeTransport, list[float]]:
    """A printer over a fake transport, with a fake clock."""
    slept: list[float] = []
    transport = FakeTransport(fail_after_bytes=cfg.pop("fail_after_bytes", None))
    printer = PhomemoD30(
        transport,
        config=D30Config(**cfg),
        sleep=slept.append,
    )
    return printer, transport, slept


def _blank(width_px: int, height_px: int) -> DeviceRaster:
    return DeviceRaster(width_px, height_px, b"\x00" * (width_px // 8 * height_px))


# --------------------------------------------------------------------------- #
# The one externally-verified fact in the whole project.
# --------------------------------------------------------------------------- #


def test_known_good_header_is_reproduced_byte_for_byte():
    """12 bytes wide, 320 lines: the header captured from the Android app."""
    assert print_header(12, 320).hex() == "1f1124001b401d7630000c004001"


def test_fifteen_mil_only_changes_the_width_field():
    assert print_header(15, 320).hex() == "1f1124001b401d7630000f004001"


def test_header_fields_are_little_endian_16bit():
    header = print_header(0x1234, 0x5678)
    assert header[-4:] == bytes.fromhex("3412") + bytes.fromhex("7856")


def test_a_frame_taller_than_the_16bit_field_is_refused():
    with pytest.raises(ValueError, match="exceeds the 16-bit"):
        print_header(15, 70000)


# --------------------------------------------------------------------------- #
# Session setup
# --------------------------------------------------------------------------- #


def test_init_is_seven_separately_flushed_writes():
    """The printer is picky about framing; these must not be coalesced."""
    printer, transport, _ = _printer(inter_packet_delay_s=0)
    printer.connect()

    assert len(transport.writes) == 7
    assert len(transport.flushes) == 7
    assert transport.buf == b"".join(INIT_PACKETS)


def test_init_sequence_content_is_pinned():
    printer, transport, _ = _printer(inter_packet_delay_s=0)
    printer.connect()
    assert transport.buf.hex() == ("1f11381f11121f11131f11091f11111f11191f11071f110a1f110202")


def test_connect_paces_the_init_packets():
    printer, _, slept = _printer(inter_packet_delay_s=0.02)
    printer.connect()
    assert slept == [0.02] * 7


def test_printing_before_connecting_is_a_programming_error():
    printer, _, _ = _printer()
    with pytest.raises(D30NotReady, match="connect"):
        printer.print_raster(_blank(120, 32))


def test_close_is_idempotent_and_never_raises():
    printer, transport, _ = _printer(inter_packet_delay_s=0)
    printer.connect()
    printer.close()
    printer.close()
    assert transport.closed == 1
    assert not printer.is_connected


def test_context_manager_connects_and_closes():
    printer, transport, _ = _printer(inter_packet_delay_s=0)
    with printer:
        assert printer.is_connected
    assert transport.closed == 1


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #


def test_a_strip_is_sent_as_exactly_one_frame():
    """The whole economy of strip mode: one leader/trailer for the batch."""
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    labels = [Image.new("L", (mm_to_px(40), mm_to_px(15)), 255) for _ in range(20)]
    raster = to_device(concat_strip(labels, 2.0))

    with printer:
        printer.print_raster(raster, wait=False)

    assert len(find_frames(bytes(transport.buf))) == 1


def test_discrete_printing_costs_a_frame_per_label():
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    raster = _blank(120, 320)

    with printer:
        for _ in range(3):
            printer.print_raster(raster, wait=False)

    assert len(find_frames(bytes(transport.buf))) == 3


def test_body_length_matches_the_header_claim():
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    raster = _blank(120, 100)

    with printer:
        printer.print_raster(raster, wait=False)

    stream = bytes(transport.buf)
    offset = find_frames(stream)[0]
    assert len(stream) - (offset + 8) == raster.width_bytes * raster.height_px


def test_a_raster_too_tall_for_one_frame_is_refused_not_silently_split():
    printer, _, _ = _printer(inter_packet_delay_s=0)
    with printer:
        with pytest.raises(D30GeometryError, match="exceeds one frame"):
            printer.print_raster(_blank(120, 70000))


# --------------------------------------------------------------------------- #
# Pacing. Only long strips hit this, which is why it needs its own test.
# --------------------------------------------------------------------------- #


def test_writes_are_chunked():
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0, chunk_bytes=1024)
    raster = _blank(120, 1000)  # 15KB

    with printer:
        printer.print_raster(raster, wait=False)

    body_writes = transport.writes[7 + 1 :]  # 7 init packets, then the header
    assert len(body_writes) == 15
    assert sum(body_writes) == len(raster.data)


def test_pacing_tracks_the_print_speed():
    """Writes must not outrun the head, or the printer's buffer overruns."""
    printer, _, slept = _printer(inter_packet_delay_s=0, pace_factor=1.0, chunk_bytes=1500)
    raster = _blank(120, 1000)

    with printer:
        printer.print_raster(raster, wait=False)

    # 15000 bytes / 15 bytes per line = 1000 lines of physical printing.
    assert sum(slept) == pytest.approx(1000 / LINES_PER_SECOND, rel=0.01)


def test_pace_factor_zero_disables_throttling():
    printer, _, slept = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(_blank(120, 1000), wait=False)
    assert slept == []


def test_wait_sleeps_for_the_physical_print_duration():
    """There is no acknowledgement, so elapsed time is the only completion signal."""
    printer, _, slept = _printer(inter_packet_delay_s=0, pace_factor=0, post_print_margin_s=0.3)
    raster = _blank(120, mm_to_px(40))

    with printer:
        printer.print_raster(raster, wait=True)

    expected = raster.height_px / LINES_PER_SECOND + 0.3
    assert slept[-1] == pytest.approx(expected)


def test_print_duration_matches_the_60mm_per_second_spec():
    printer, _, _ = _printer()
    forty_mm = _blank(120, mm_to_px(40))
    assert printer.print_duration_s(forty_mm) == pytest.approx(40 / 60, rel=0.01)


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_a_mid_strip_disconnect_surfaces_as_retryable():
    printer, transport, _ = _printer(
        inter_packet_delay_s=0, pace_factor=0, chunk_bytes=512, fail_after_bytes=2000
    )
    with printer:
        with pytest.raises(D30ConnectError) as caught:
            printer.print_raster(_blank(120, 1000), wait=False)

    assert caught.value.retryable
    # Some bytes made it out: tape has moved and the caller cannot know how far.
    assert len(transport.buf) == 2000


def test_writing_to_a_closed_transport_raises():
    transport = FakeTransport()
    with pytest.raises(D30ConnectError, match="not open"):
        transport.write(b"x")


# --------------------------------------------------------------------------- #
# decode: proves the bytes, not the intent.
# --------------------------------------------------------------------------- #


def test_decode_round_trips_a_rendered_label():
    label = LabelSpec(elements=[TextElement(value="ROUND TRIP")], length_mm=40)
    original = render_label(label, TAPE_15)
    raster = to_device(original)

    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(raster, wait=False)

    rebuilt = decode(bytes(transport.buf))

    assert rebuilt.size == original.size
    # Compare after the same 1-bit threshold the printer would have applied.
    from labelfab.render import to_bilevel

    expected = to_bilevel(original).point(lambda p: 255 - p).convert("1")
    assert rebuilt.convert("1").tobytes() == expected.tobytes()


def test_decode_survives_a_qr_and_it_still_scans():
    zxingcpp = pytest.importorskip("zxingcpp")
    label = LabelSpec(elements=[QrElement(value="SI4821")], length_mm=25)
    raster = to_device(render_label(label, TAPE_15))

    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(raster, wait=False)

    result = zxingcpp.read_barcode(decode(bytes(transport.buf)))
    assert result and result.text == "SI4821"


def test_decode_reports_the_frame_geometry():
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(_blank(120, 640), wait=False)

    frames = decode_frames(bytes(transport.buf))
    assert len(frames) == 1
    assert (frames[0].width_px, frames[0].height_px, frames[0].width_bytes) == (120, 640, 15)


def test_decode_rejects_a_truncated_frame():
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(_blank(120, 100), wait=False)

    with pytest.raises(DecodeError, match="only"):
        decode(bytes(transport.buf)[:-50])


def test_decode_complains_when_a_batch_was_not_a_strip():
    """A multi-frame capture means the batch paid a feed per label."""
    printer, transport, _ = _printer(inter_packet_delay_s=0, pace_factor=0)
    with printer:
        printer.print_raster(_blank(120, 32), wait=False)
        printer.print_raster(_blank(120, 32), wait=False)

    with pytest.raises(DecodeError, match="leader/trailer feed per label"):
        decode(bytes(transport.buf))


def test_decode_rejects_something_that_is_not_a_capture():
    with pytest.raises(DecodeError, match="no GS v 0 frame"):
        decode(b"hello world")


# --------------------------------------------------------------------------- #
# Self test pattern
# --------------------------------------------------------------------------- #


def test_self_test_pattern_has_a_complete_border():
    printer, _, _ = _printer()
    raster = printer.self_test(120, 200)
    rebuilt = Image.frombytes("1", (raster.width_px, raster.height_px), raster.data)

    assert all(rebuilt.getpixel((x, 0)) for x in range(120)), "top edge"
    assert all(rebuilt.getpixel((x, 199)) for x in range(120)), "bottom edge"
    assert all(rebuilt.getpixel((0, y)) for y in range(200)), "left edge"
    assert all(rebuilt.getpixel((119, y)) for y in range(200)), "right edge"


def test_self_test_is_asymmetric_so_rotation_is_unambiguous():
    printer, _, _ = _printer()
    raster = printer.self_test(120, 200)
    img = Image.frombytes("1", (raster.width_px, raster.height_px), raster.data)
    assert img.getpixel((5, 4)) and not img.getpixel((114, 195))


def test_self_test_rejects_a_non_byte_aligned_width():
    printer, _, _ = _printer()
    with pytest.raises(D30GeometryError, match="whole number of bytes"):
        printer.self_test(100, 200)
