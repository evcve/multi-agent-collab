#!/usr/bin/env python3
"""FEM (Nastran/OptiStruct .fem/.bdf) RBE2 mount-hole extractor.

In structural FE models, equipment masses are CONM2 point masses connected to the
panel via RBE2 rigid elements. The RBE2 *dependent* nodes are the bolt-hole
centers projected onto the panel — i.e. the exact mount-hole positions.

Key parser details (classic footguns):
- Nastran uses FIXED-COLUMN format (8 chars per field): "221.952944.6044" is
  actually two adjacent numbers glued together. Splitting on whitespace is WRONG.
- RBE2 continuation lines: field 7 == '+' means the dependent-node list continues
  on the next line.
- Mass units are often tonne (0.002987 t = 2.987 kg), coords in mm.

Usage:
    python fem_rbe2_holes.py <model.fem> [--nodes 45,44,43,...]

Output: for each CONM2 node id, the RBE2 dependent-node coordinates.
"""
import argparse
import json
import sys


def field(line, i):
    """Fixed-column field i (0-based): columns [8+8i, 16+8i)."""
    return line[8 + 8 * i: 16 + 8 * i].strip()


def parse(fem_path):
    grids = {}
    conm2 = {}
    rbe2 = {}   # gn -> [dependent node ids]

    with open(fem_path, "r", errors="replace") as f:
        lines = f.readlines()

    i, n = 0, len(lines)
    while i < n:
        s = lines[i].rstrip("\n")
        if s.startswith("GRID"):
            try:
                gid = int(field(s, 0))
                grids[gid] = (
                    float(field(s, 2)) or 0.0,
                    float(field(s, 3)) or 0.0,
                    float(field(s, 4)) or 0.0,
                )
            except ValueError:
                pass
            i += 1
        elif s.startswith("CONM2"):
            try:
                g = int(field(s, 1))
                if field(s, 2):          # 带 CID：M 在字段 4
                    m = float(field(s, 4)) if field(s, 4) else 0.0
                else:                    # 无 CID：M 在字段 3
                    m = float(field(s, 3)) if field(s, 3) else 0.0
                conm2[g] = m
            except ValueError:
                pass
            i += 1
        elif s.startswith("RBE2"):
            parts = [s]
            while field(s, 7) == "+":
                i += 1
                s = lines[i].rstrip("\n")
                parts.append(s)
            try:
                gn = int(field(parts[0], 1))
                deps = []
                for p in parts:
                    for fld in range(3, 8):
                        v = field(p, fld)
                        if v and v != "+":
                            try:
                                deps.append(int(v))
                            except ValueError:
                                pass
                rbe2.setdefault(gn, []).extend(deps)
            except ValueError:
                pass
            i += 1
        else:
            i += 1
    return grids, conm2, rbe2


def main():
    ap = argparse.ArgumentParser(description="Extract RBE2 mount holes from Nastran FEM")
    ap.add_argument("fem")
    ap.add_argument("--nodes", help="comma-separated node ids to report (default: all)")
    args = ap.parse_args()

    grids, conm2, rbe2 = parse(args.fem)
    wanted = [int(x) for x in args.nodes.split(",")] if args.nodes else sorted(rbe2)

    result = {}
    for gn in wanted:
        deps = rbe2.get(gn, [])
        holes = []
        for d in deps:
            if d in grids:
                x, y, z = grids[d]
                holes.append({"node": d, "x": x, "y": y, "z": z,
                              "mass_kg": (conm2.get(gn) or 0.0) * 1000})
        if holes:
            result[gn] = holes

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
