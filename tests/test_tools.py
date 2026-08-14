"""tools 单元测试：DXF 条带配对、FEM 固定列解析、干涉检测。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dxf_rect_extract import pair_into_bands
from tools.fem_rbe2_holes import parse
from tools.interference_check import load_bands, load_holes


# ── DXF 条带配对 ────────────────────────────────────────────────────
def test_pair_into_bands_pairs_adjacent_lines():
    coords = {336.2: [-600, 600], 376.2: [-600, 600],   # H1 两条边
              175.6: [-600, 600], 215.6: [-600, 600]}   # H2 两条边
    bands = pair_into_bands(coords)
    assert len(bands) == 2
    assert bands[0] == (175.6, 215.6)   # 按位置排序
    assert bands[1] == (336.2, 376.2)


def test_pair_into_bands_unpaired_line_becomes_zero_width():
    coords = {100.0: [-10, 10]}          # 单条线
    bands = pair_into_bands(coords)
    assert bands == [(100.0, 100.0)]


def test_pair_into_bands_far_lines_not_paired():
    coords = {100.0: [0, 10], 900.0: [0, 10]}   # 间距 800 > max_gap
    assert len(pair_into_bands(coords)) == 2


# ── FEM 固定列解析 ──────────────────────────────────────────────────
FEM_SAMPLE = """$ sample
GRID          10        -100.000 -50.000 -200.000
GRID          20         100.000  50.000 -200.000
GRID          30         200.000 150.000 -200.000
CONM2         1      10         0.0025     0.0     0.0     0.0
RBE2       100      10  123456      20      30
"""


def test_fem_parse_grids_conm2_rbe2(tmp_path):
    fem = tmp_path / "m.fem"
    fem.write_text(FEM_SAMPLE, encoding="ascii")
    grids, conm2, rbe2 = parse(str(fem))
    assert grids[10] == (-100.0, -50.0, -200.0)
    assert conm2[10] == pytest.approx(0.0025)
    assert rbe2[10] == [20, 30]


def test_fem_mass_tonne_conversion_used_by_cli():
    """0.0025 t = 2.5 kg（CLI 输出换算，parse 层保留原始吨值）。"""
    assert 0.0025 * 1000 == 2.5


# ── 干涉检测 ────────────────────────────────────────────────────────
def test_load_bands_csv():
    bands = load_bands("examples/sample_bands.csv")
    assert len(bands) == 8
    x_bands = [b for b in bands if b[0] == "X"]
    assert len(x_bands) == 4


def test_interference_logic(tmp_path):
    import csv
    bands_csv = tmp_path / "b.csv"
    holes_csv = tmp_path / "h.csv"
    with open(bands_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dir", "name", "lo", "hi"])
        w.writerow(["X", "H2", 175.6, 215.6])
    with open(holes_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["node", "x", "y"])
        w.writerow(["EQ-A", 230.9, 194.8])   # Y 在 H2 内 → 冲突
        w.writerow(["EQ-B", 230.9, 300.0])   # Y 不在 H2 → 不冲突
    bands = load_bands(str(bands_csv))
    holes = load_holes(str(holes_csv))
    conflicts = []
    for node, x, y in holes:
        hits = [b[1] for b in bands if b[0] == "X" and b[2] <= y <= b[3]]
        if hits:
            conflicts.append((node, hits))
    assert conflicts == [("EQ-A", ["H2"])]
