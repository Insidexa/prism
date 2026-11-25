from pathlib import Path

from prism.sensors import thermal_zone_temp_c


def test_zone_by_type(tmp_path: Path):
    for i, (t, v) in enumerate((("x86_pkg", "50000"), ("acpitz", "43000"))):
        z = tmp_path / f"thermal_zone{i}"
        z.mkdir()
        (z / "type").write_text(t + "\n")
        (z / "temp").write_text(v + "\n")
    assert thermal_zone_temp_c("acpitz", tmp_path) == 43.0
    assert thermal_zone_temp_c("missing", tmp_path) is None
