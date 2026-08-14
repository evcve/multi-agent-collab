#!/usr/bin/env python3
"""Interference check: mount holes vs. rectangular bands (e.g. heat-pipe strips).

Given hole coordinates (CSV: node,x,y) and bands (JSON from dxf_rect_extract or CSV),
report every hole that falls inside any band (with optional safety margin).

Usage:
    python interference_check.py --holes holes.csv --bands bands.csv [--margin 8]

holes.csv:  node,x,y
bands.csv:  dir,name,lo,hi   (dir=X means horizontal band spanning Y in [lo,hi];
                              dir=Y means vertical band spanning X in [lo,hi])
"""
import argparse
import csv


def load_holes(path):
    holes = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            holes.append((row["node"], float(row["x"]), float(row["y"])))
    return holes


def load_bands(path):
    bands = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bands.append((row["dir"], row["name"], float(row["lo"]), float(row["hi"])))
    return bands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holes", required=True)
    ap.add_argument("--bands", required=True)
    ap.add_argument("--margin", type=float, default=8.0,
                    help="safety distance from band edge (mm)")
    args = ap.parse_args()

    holes = load_holes(args.holes)
    bands = load_bands(args.bands)
    conflicts = []

    for node, x, y in holes:
        hits = []
        for d, name, lo, hi in bands:
            if d == "X":            # horizontal band: check Y
                if lo + args.margin <= y <= hi - args.margin:
                    hits.append(name)
            else:                   # vertical band: check X
                if lo + args.margin <= x <= hi - args.margin:
                    hits.append(name)
        if hits:
            conflicts.append((node, x, y, hits))

    print(f"# {len(holes)} holes, {len(conflicts)} conflicts")
    for node, x, y, hits in conflicts:
        print(f"{node}: ({x:.1f},{y:.1f}) -> {','.join(hits)}")

    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
