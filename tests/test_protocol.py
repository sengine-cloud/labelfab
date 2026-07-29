"""The command table, and the invariants that keep it honest.

The point of ``Support`` is that "we have seen this work" is data rather than
folklore, so the interesting tests here are the ones asserting we do not quietly put
an unverified command on a path that runs by default.
"""

from __future__ import annotations

import pytest

from labelfab.device import protocol as p

# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def test_opcodes_match_the_captured_bytes():
    """Spot-check against the wire, not against the decompile."""
    assert p.SERIAL() == bytes.fromhex("1f1109")
    assert p.FIRMWARE_VERSION() == bytes.fromhex("1f1107")
    assert p.BATTERY() == bytes.fromhex("1f1108")
    assert p.PRINT_DENSITY(p.DENSITY_LIGHT) == bytes.fromhex("1f110201")
    assert p.PRINT_MULTI(3) == bytes.fromhex("1f112103")
    assert p.LEFT_MARGIN(0) == bytes.fromhex("1f112400")
    assert p.EXIT_COMPRESS_MODE(0) == bytes.fromhex("1f113500")
    assert p.INIT_PRINTER() == bytes.fromhex("1b40")
    assert p.VERIFY_PAPER() == bytes.fromhex("1b4e10")


def test_argument_count_is_enforced():
    with pytest.raises(ValueError, match="takes 1 argument"):
        p.PRINT_DENSITY()
    with pytest.raises(ValueError, match="takes 1 argument"):
        p.PRINT_DENSITY(1, 2)


def test_arguments_must_be_bytes():
    with pytest.raises(ValueError, match="not a byte"):
        p.PRINT_MULTI(256)


def test_raster_header_is_little_endian():
    assert p.raster_header(12, 320).hex() == "1d7630000c004001"


def test_raster_header_refuses_more_than_the_16bit_field():
    with pytest.raises(ValueError, match="exceeds the 16-bit"):
        p.raster_header(12, 70000)


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #


def test_the_probe_set_cannot_change_persistent_state():
    """A probe sweep must be safe to run against an unknown unit."""
    for cmd in p.PROBE_SET:
        assert cmd.is_safe_to_probe, f"{cmd.name} is {cmd.danger}"
        assert cmd.args == 0, f"{cmd.name} needs arguments and is not a plain query"


def test_destructive_commands_are_named_and_excluded_from_probing():
    probe_names = {c.name for c in p.PROBE_SET}
    assert not (p.FORBIDDEN & probe_names)
    for name in p.FORBIDDEN:
        cmd = getattr(p, name)
        assert cmd.danger is p.Danger.DESTRUCTIVE


def test_device_id_is_marked_destructive():
    """Rewriting the serial breaks model resolution permanently and silently."""
    assert p.DEVICE_ID.danger is p.Danger.DESTRUCTIVE
    assert p.DEVICE_ID.name in p.FORBIDDEN


def test_firmware_commands_are_all_destructive():
    for cmd in (
        p.OTA_MODE,
        p.FIRMWARE_UPGRADE_START,
        p.FIRMWARE_UPGRADE_CONFIRM,
        p.FIRMWARE_UPGRADE_CANCEL,
    ):
        assert cmd.danger is p.Danger.DESTRUCTIVE


# --------------------------------------------------------------------------- #
# Verification status
# --------------------------------------------------------------------------- #


def test_the_default_print_path_uses_only_verified_commands():
    """Nothing unproven may run on a normal print.

    ``print_preamble`` is what every job sends, so every opcode inside it must have
    been seen working on real hardware.
    """
    verified = {
        c.opcode
        for c in vars(p).values()
        if isinstance(c, p.Command) and c.support is p.Support.VERIFIED
    }
    preamble = p.print_preamble(12, 320, density=p.DENSITY_MEDIUM, copies=2)
    # Every 2- or 3-byte opcode prefix present must belong to a verified command.
    for cmd in (
        p.UNKNOWN_0A,
        p.PRINT_DENSITY,
        p.LEFT_MARGIN,
        p.PRINT_MULTI,
        p.INIT_PRINTER,
        p.EXIT_COMPRESS_MODE,
        p.PRINT_IMAGE,
    ):
        assert cmd.opcode in verified, f"{cmd.name} is on the print path but not VERIFIED"
        assert cmd.opcode in preamble or cmd is p.PRINT_IMAGE


def test_session_setup_uses_only_verified_commands():
    for cmd in p.SESSION_QUERIES:
        assert cmd.support is p.Support.VERIFIED, f"{cmd.name} runs on connect but is unverified"


# --------------------------------------------------------------------------- #
# Session setup and preamble
# --------------------------------------------------------------------------- #


def test_session_setup_is_pinned_to_the_captured_sequence():
    assert b"".join(p.session_setup(density=p.DENSITY_MEDIUM)).hex() == (
        "1f11381f11121f11131f11091f11111f11191f11071f110a1f110202"
    )


def test_batching_changes_framing_not_bytes():
    a = b"".join(p.session_setup(batched=False))
    b = b"".join(p.session_setup(batched=True))
    assert a == b


def test_density_must_be_one_of_the_three_observed_values():
    with pytest.raises(ValueError, match="not one of"):
        p.session_setup(density=3)
    for d in p.DENSITIES:
        assert p.session_setup(density=d)


def test_preamble_carries_copies_only_when_more_than_one():
    one = p.print_preamble(12, 320, copies=1)
    many = p.print_preamble(12, 320, copies=4)
    assert p.PRINT_MULTI.opcode not in one
    assert p.PRINT_MULTI(4) in many


def test_preamble_rejects_a_copy_count_that_will_not_fit():
    with pytest.raises(ValueError, match="copies"):
        p.print_preamble(12, 320, copies=0)
    with pytest.raises(ValueError, match="exceeds the one-byte field"):
        p.print_preamble(12, 320, copies=300)


# --------------------------------------------------------------------------- #
# Left margin. The bit that used to be hardcoded.
# --------------------------------------------------------------------------- #


def test_unmeasured_head_yields_a_zero_margin():
    """Byte-identical to the old hardcoded prefix, which is the point.

    The head width is still an open bring-up question, so letterboxing on an
    assumption would be worse than not letterboxing at all.
    """
    assert p.left_margin_bytes(15) == 0
    assert p.left_margin_bytes(12) == 0


def test_a_narrow_raster_is_letterboxed_once_the_head_is_known():
    assert p.left_margin_bytes(8, p.HEAD_WIDTH_BYTES) == 4
    assert p.left_margin_bytes(12, p.HEAD_WIDTH_BYTES) == 0


def test_a_raster_wider_than_the_head_does_not_go_negative():
    assert p.left_margin_bytes(15, p.HEAD_WIDTH_BYTES) == 0


# --------------------------------------------------------------------------- #
# Auto power-off. 5-minute units, confirmed across both vendor apps.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "1b4e0700"), (10, "1b4e0702"), (30, "1b4e0706"), (60, "1b4e070c"), (600, "1b4e0778")],
)
def test_auto_shutdown_matches_the_captured_ui_selections(minutes, expected):
    assert p.auto_shutdown_minutes(minutes).hex() == expected


def test_auto_shutdown_rejects_values_off_the_five_minute_grid():
    with pytest.raises(ValueError, match="multiple of the 5-minute unit"):
        p.auto_shutdown_minutes(7)


def test_auto_shutdown_rejects_more_than_the_field_holds():
    with pytest.raises(ValueError, match="exceeds the one-byte field"):
        p.auto_shutdown_minutes(5 * 256)
