#!/usr/bin/env python3
"""生成示例数据文件（sample_bands.dxf / sample_model.fem / 更新 sample_*.csv）。
示例数据为合成数据，不代表任何真实项目。运行: python examples/make_sample_data.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def make_dxf(path):
    """生成包含 8 个正交条带（4横4竖）的 ASCII DXF。"""
    bands_x = [("H1", 336.2, 376.2), ("H2", 175.6, 215.6),
               ("H3", -210.4, -170.4), ("H4", -342.3, -302.3)]
    bands_y = [("V1", -603.4, -563.4), ("V2", -198.4, -158.4),
               ("V3", 195.2, 235.2), ("V4", 558.1, 598.1)]
    lines = []
    # 横条带上下边
    for name, a, b in bands_x:
        lines.append(("H", a, b))
    # 竖条带左右边
    for name, a, b in bands_y:
        lines.append(("V", a, b))
    with open(path, "w", encoding="ascii") as f:
        f.write("0\nSECTION\n2\nENTITIES\n")
        for kind, a, b in lines:
            if kind == "H":  # horizontal: y=a, y=b
                for y in (a, b):
                    f.write(f"0\nLINE\n8\n0\n10\n-600.0\n20\n{y:.1f}\n11\n600.0\n21\n{y:.1f}\n")
            else:          # vertical: x=a, x=b
                for x in (a, b):
                    f.write(f"0\nLINE\n8\n0\n10\n{x:.1f}\n20\n-450.0\n11\n{x:.1f}\n21\n450.0\n")
        f.write("0\nENDSEC\n0\nEOF\n")
    print(f"生成 {path}")


def make_fem(path):
    """生成带 GRID/CONM2/RBE2 的最小 Nastran 模型（固定列格式，每字段 8 列）。"""
    lines = []
    # 板面节点（RBE2 dependent = 安装孔投影点）
    panel = {
        101: (-440.0, -60.0), 102: (-440.0, -20.0),
        103: (-668.0, -60.0), 104: (-668.0, -20.0),
        105: (230.9, 194.8),  106: (248.8, 194.8),
        107: (200.0, 190.0),  108: (213.9, 194.8),
        109: (222.0, 390.0),  110: (222.0, 470.0),
        111: (-535.0, -309.0), 112: (-667.5, -309.0),
    }
    for gid, (x, y) in panel.items():
        lines.append(f"{'GRID':8s}{gid:8d}{'':8s}{x:8.3f}{y:8.3f}{-211.5:8.3f}")
    # 质心节点（CONM2 挂载 = 单机质心；RBE2 independent）
    units = {
        13: (-561.4, -42.0, 0.0015),   # 流体控制器 1.5kg
        31: (-378.9, 379.4, 0.0040),   # 蓄电池 4kg
        45: (222.0, 44.6, 0.00299),    # 综合电子 2.99kg
    }
    eid = 1
    for gid, (x, y, m) in units.items():
        lines.append(f"{'GRID':8s}{gid:8d}{'':8s}{x:8.3f}{y:8.3f}{-160.0:8.3f}")
        lines.append(f"{'CONM2':8s}{eid:8d}{gid:8d}{m:8.6f}{'':8s}{'':8s}{'':8s}")
        eid += 1
    # RBE2：质心节点 -> 安装孔（dependent 板节点）
    rbe2 = {13: [101, 102, 103, 104], 31: [109, 110, 101, 102],
            45: [105, 106, 107, 108]}
    eid = 100
    for gn, deps in rbe2.items():
        line = f"{'RBE2':8s}{eid:8d}{gn:8d}{'123456':8s}" + "".join(f"{d:8d}" for d in deps)
        lines.append(line)
        eid += 1
    with open(path, "w", encoding="ascii") as f:
        f.write("$ Example Nastran model (synthetic data)\n")
        for l in lines:
            f.write(l + "\n")
    print(f"生成 {path}")


if __name__ == "__main__":
    make_dxf(os.path.join(HERE, "sample_bands.dxf"))
    make_fem(os.path.join(HERE, "sample_model.fem"))
