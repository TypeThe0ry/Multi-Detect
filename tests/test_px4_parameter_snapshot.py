import pytest

from multidetect.simulation.px4_parameter_snapshot import parse_parameter_observation


def observation(text):
    return dict(returncode=0, output_truncated=False, stdout=text)


def test_actual_show_shape_parsed_without_picking_index_or_used_count():
    text = "Symbols: x = used\nx     FW_R_LIM [616,796] : 50.0000\n\n 1335/2048 parameters used.\n"
    assert parse_parameter_observation("FW_R_LIM", observation(text)) == 50


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x OTHER [1,2] : 50",
        "x FW_R_LIM [1,2] : nan",
        "x FW_R_LIM [1,2] : inf",
        "x FW_R_LIM [1,2] : 50\nx FW_R_LIM [1,2] : 40",
        "x FW_R_LIM [1,2] : 50 trailing",
        "FW_R_LIM not found",
    ],
)
def test_missing_ambiguous_or_nonfinite_value_rejected(text):
    with pytest.raises(ValueError):
        parse_parameter_observation("FW_R_LIM", observation(text))


@pytest.mark.parametrize(
    "field,value", [("returncode", 1), ("returncode", False), ("output_truncated", True)]
)
def test_failed_or_truncated_read_rejected(field, value):
    record = observation("x FW_R_LIM [1,2] : 50")
    record[field] = value
    with pytest.raises(ValueError):
        parse_parameter_observation("FW_R_LIM", record)
