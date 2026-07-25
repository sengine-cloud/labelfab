import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from labelfab.contract import (
    LabelSpec,
    PrintJob,
    PrintOptions,
    TapeSpec,
    job_schema,
    mm_to_px,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "job-v1.schema.json"


def _minimal(**kw) -> dict:
    job = {
        "job_id": "01JZ",
        "idempotency_key": "k1",
        "printer": {"id": "d30-workshop"},
        "labels": [{"preset": "stock_item", "vars": {"code": "SI1"}}],
    }
    job.update(kw)
    return job


# --------------------------------------------------------------------------- #
# Geometry: these three numbers are why the reference's magic header works.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mm", "px"),
    [(12, 96), (15, 120), (40, 320)],
)
def test_millimetres_map_to_the_reference_pixel_counts(mm, px):
    assert mm_to_px(mm) == px


def test_both_tape_widths_are_byte_aligned():
    for mm in (12, 15):
        assert mm_to_px(mm) % 8 == 0


# --------------------------------------------------------------------------- #
# Validation rules
# --------------------------------------------------------------------------- #


def test_label_needs_exactly_one_of_preset_or_elements():
    with pytest.raises(ValidationError, match="exactly one"):
        LabelSpec()
    with pytest.raises(ValidationError, match="exactly one"):
        LabelSpec(preset="p", elements=[{"type": "text", "value": "x"}])


def test_vars_without_preset_is_rejected():
    with pytest.raises(ValidationError, match="only meaningful alongside"):
        LabelSpec(elements=[{"type": "text", "value": "x"}], vars={"a": "b"})


def test_strip_mode_is_refused_on_die_cut_tape():
    with pytest.raises(ValidationError, match="requires continuous tape"):
        PrintJob(**_minimal(tape={"kind": "gap"}))


def test_discrete_mode_is_allowed_on_die_cut_tape():
    job = PrintJob(**_minimal(tape={"kind": "gap"}, options={"batch_mode": "discrete"}))
    assert job.tape.kind == "gap"


def test_defaults_are_fifteen_mil_continuous_strip():
    job = PrintJob(**_minimal())
    assert (job.tape.width_mm, job.tape.kind) == (15.0, "continuous")
    assert job.tape.length_mm == "auto"
    assert job.options.batch_mode == "strip"


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        PrintJob(**_minimal(nonsense=1))


def test_barcode_module_width_floor_is_enforced():
    """Below 0.25mm (2 device px) scanners fail; refuse rather than print garbage."""
    with pytest.raises(ValidationError):
        LabelSpec(elements=[{"type": "barcode", "value": "X", "module_width_mm": 0.2}])


def test_text_point_range_must_be_ordered():
    with pytest.raises(ValidationError, match="exceeds max_pt"):
        LabelSpec(elements=[{"type": "text", "value": "x", "min_pt": 20, "max_pt": 10}])


def test_element_union_discriminates_on_type():
    label = LabelSpec(
        elements=[
            {"type": "qr", "value": "SI1"},
            {"type": "text", "value": "bolt"},
        ]
    )
    assert [e.type for e in label.elements] == ["qr", "text"]


def test_nested_boxes_round_trip():
    label = LabelSpec(
        elements=[
            {
                "type": "box",
                "direction": "col",
                "children": [
                    {"type": "text", "value": "top"},
                    {"type": "text", "value": "bottom"},
                ],
            }
        ]
    )
    assert label.elements[0].children[1].value == "bottom"


def test_job_round_trips_through_json():
    job = PrintJob(**_minimal(options={"flush": True}, tape={"width_mm": 12}))
    assert PrintJob.model_validate_json(job.model_dump_json()) == job


def test_models_are_frozen():
    with pytest.raises(ValidationError):
        PrintOptions().flush = True
    with pytest.raises(ValidationError):
        TapeSpec().width_mm = 12


# --------------------------------------------------------------------------- #
# Schema snapshot. A change here is a cross-repo contract change, so make it loud.
# --------------------------------------------------------------------------- #


def test_committed_schema_matches_the_models():
    current = job_schema()
    if os.environ.get("LABELFAB_UPDATE_GOLDEN"):
        SCHEMA_PATH.parent.mkdir(exist_ok=True)
        SCHEMA_PATH.write_text(json.dumps(current, indent=2) + "\n")
    committed = json.loads(SCHEMA_PATH.read_text())
    assert committed == current, (
        "The job contract changed. If deliberate, regenerate with "
        "LABELFAB_UPDATE_GOLDEN=1 pytest and update every producer."
    )
