#!/usr/bin/env python3
"""Extract simplified Gyeongnam city/county outlines from the official WKB CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
from collections import defaultdict
from pathlib import Path


DISTRICT_ORDER = (
    "창원", "진주", "통영", "사천", "김해", "밀양", "거제", "양산",
    "의령", "함안", "창녕", "고성", "남해", "하동", "산청", "함양",
    "거창", "합천",
)


class WkbReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def geometry(self):
        endian = "<" if self.read(1) == b"\x01" else ">"
        geometry_type = struct.unpack(endian + "I", self.read(4))[0] & 0xFF
        if geometry_type == 1:
            return ("point", struct.unpack(endian + "dd", self.read(16)))
        if geometry_type == 2:
            count = struct.unpack(endian + "I", self.read(4))[0]
            return ("line", [struct.unpack(endian + "dd", self.read(16)) for _ in range(count)])
        if geometry_type == 3:
            ring_count = struct.unpack(endian + "I", self.read(4))[0]
            rings = []
            for _ in range(ring_count):
                count = struct.unpack(endian + "I", self.read(4))[0]
                rings.append([struct.unpack(endian + "dd", self.read(16)) for _ in range(count)])
            return ("polygon", rings)
        if geometry_type in (4, 5, 6, 7):
            count = struct.unpack(endian + "I", self.read(4))[0]
            return ("collection", [self.geometry() for _ in range(count)])
        raise ValueError(f"Unsupported WKB geometry type: {geometry_type}")


def outer_rings(geometry) -> list[list[tuple[float, float]]]:
    kind, value = geometry
    if kind == "polygon":
        return [value[0]] if value else []
    if kind == "collection":
        return [ring for child in value for ring in outer_rings(child)]
    return []


def point_line_distance(point, start, end) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
    projection = (start[0] + t * dx, start[1] + t * dy)
    return math.hypot(point[0] - projection[0], point[1] - projection[1])


def simplify_line(points, tolerance: float):
    if len(points) <= 2:
        return points
    max_distance, index = 0.0, 0
    for candidate_index, point in enumerate(points[1:-1], start=1):
        distance = point_line_distance(point, points[0], points[-1])
        if distance > max_distance:
            max_distance, index = distance, candidate_index
    if max_distance <= tolerance:
        return [points[0], points[-1]]
    left = simplify_line(points[:index + 1], tolerance)
    right = simplify_line(points[index:], tolerance)
    return left[:-1] + right


def ring_area(points) -> float:
    return abs(sum(points[index][0] * points[(index + 1) % len(points)][1] - points[(index + 1) % len(points)][0] * points[index][1] for index in range(len(points))) / 2)


def simplify_ring(points, tolerance: float):
    if points and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 4 or ring_area(points) < 0.0000008:
        return []
    simplified = simplify_line(points, tolerance)
    if len(simplified) < 3:
        return []
    simplified.append(simplified[0])
    return [[round(point[0], 5), round(point[1], 5)] for point in simplified]


def district_name(raw_name: str) -> str:
    if raw_name.startswith("창원시"):
        return "창원"
    return raw_name.removesuffix("시").removesuffix("군")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--boundary-csv", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("tools/gn-boundaries.js"))
    parser.add_argument("--tolerance", type=float, default=0.0012)
    args = parser.parse_args()

    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    grouped: dict[str, dict] = defaultdict(lambda: {"codes": [], "polygons": []})
    with args.boundary_csv.open("r", encoding="cp949", newline="") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("시군구코드", ""))
            if not code.startswith("48"):
                continue
            name = district_name(row.get("시군구명", ""))
            if name not in DISTRICT_ORDER:
                continue
            grouped[name]["codes"].append(code)
            geometry = WkbReader(bytes.fromhex(row["공간정보"])).geometry()
            for ring in outer_rings(geometry):
                simplified = simplify_ring(ring, args.tolerance)
                if simplified:
                    grouped[name]["polygons"].append(simplified)

    features = []
    for name in DISTRICT_ORDER:
        item = grouped.get(name)
        if not item:
            continue
        item["polygons"].sort(key=ring_area, reverse=True)
        features.append({"name": name, "codes": sorted(item["codes"]), "polygons": item["polygons"]})

    point_count = sum(len(ring) for feature in features for ring in feature["polygons"])
    payload = {
        "schemaVersion": 1,
        "source": {
            "title": "국토교통부 국토지리정보원_공간정보공동활용_시군구_20230915",
            "url": "https://www.data.go.kr/data/15123131/fileData.do",
            "dataDate": "2023-09-15",
        },
        "features": features,
        "stats": {"districts": len(features), "points": point_count},
    }
    args.output.write_text("window.GN_BOUNDARY_DATA=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False))


if __name__ == "__main__":
    main()
