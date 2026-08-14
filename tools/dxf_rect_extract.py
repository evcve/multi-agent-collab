#!/usr/bin/env python3
"""DXF geometry extractor — extract horizontal/vertical bands (rect strips) from an ASCII DXF.

Use case: CAD export (e.g. UG/NX 2D Exchange DXF top-view) of heat pipes / slots / cable trays
modeled as 40mm-wide rectangular strips. This parser reads LINE entities, keeps only
orthogonal segments, and clusters them into bands by coordinate.

Usage:
    python dxf_rect_extract.py <file.dxf> [--band-width 40]

Output: JSON list of bands: [{"dir": "X", "name": "B1", "lo": 175.6, "hi": 215.6}, ...]
"""
import json
import sys


def parse_pairs(path: str):
    """Read DXF as (code, value) pairs."""
    with open(path, "r", errors="replace") as f:
        raw = f.read().splitlines()
    pairs = []
    i = 0
    while i + 1 < len(raw):
        pairs.append((raw[i].strip(), raw[i + 1].strip()))
        i += 2
    return pairs


def entities_section(pairs):
    """Return index range of ENTITIES section (group-code 2 = section name)."""
    start = end = None
    for idx, (c, v) in enumerate(pairs):
        if c == "2" and v == "ENTITIES":
            start = idx
        if start is not None and c == "0" and v == "ENDSEC":
            end = idx
            break
    if start is None or end is None:
        raise ValueError("ENTITIES section not found")
    return start, end


def extract_lines(pairs, start, end):
    """Extract LINE entities -> list of (x1,y1,x2,y2)."""
    lines = []
    j = start + 1
    while j < end:
        c, v = pairs[j]
        if c == "0":
            etype = v
            if etype == "LINE":
                d = {}
                j += 1
                while j < end and pairs[j][0] != "0":
                    d.setdefault(pairs[j][0], []).append(pairs[j][1])
                    j += 1
                lines.append((
                    float(d["10"][0]), float(d["20"][0]),
                    float(d["11"][0]), float(d["21"][0]),
                ))
            else:
                j += 1
        else:
            j += 1
    return lines


def cluster_bands(lines, tol=0.5):
    """Cluster orthogonal lines into bands.

    Horizontal lines (y1≈y2) at same Y -> band; vertical lines (x1≈x2) -> band.
    """
    h_lines = {}  # y -> [xmin, xmax]
    v_lines = {}  # x -> [ymin, ymax]
    for x1, y1, x2, y2 in lines:
        if abs(y2 - y1) < tol:
            y = round((y1 + y2) / 2, 3)
            lo, hi = min(x1, x2), max(x1, x2)
            if y in h_lines:
                h_lines[y] = [min(h_lines[y][0], lo), max(h_lines[y][1], hi)]
            else:
                h_lines[y] = [lo, hi]
        elif abs(x2 - x1) < tol:
            x = round((x1 + x2) / 2, 3)
            lo, hi = min(y1, y2), max(y1, y2)
            if x in v_lines:
                v_lines[x] = [min(v_lines[x][0], lo), max(v_lines[x][1], hi)]
            else:
                v_lines[x] = [lo, hi]
    return h_lines, v_lines


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    pairs = parse_pairs(path)
    start, end = entities_section(pairs)
    lines = extract_lines(pairs, start, end)
    h_lines, v_lines = cluster_bands(lines)

    bands = []
    for i, (y, (lo, hi)) in enumerate(sorted(h_lines.items()), 1):
        bands.append({"dir": "X", "name": f"H{i}", "lo": lo, "hi": hi, "center": y})
    for i, (x, (lo, hi)) in enumerate(sorted(v_lines.items()), 1):
        bands.append({"dir": "Y", "name": f"V{i}", "lo": lo, "hi": hi, "center": x})

    print(json.dumps(bands, indent=2))
    print(f"# {len(lines)} lines -> {len(bands)} bands", file=sys.stderr)


if __name__ == "__main__":
    main()
