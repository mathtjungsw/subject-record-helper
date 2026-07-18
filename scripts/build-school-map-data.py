#!/usr/bin/env python3
"""Build a compact school-coordinate index for the personnel history map.

The input CSV is the Ministry of the Interior and Safety standard school-location
dataset. Only Gyeongnam schools that can be matched to a person's latest known
workplace are emitted.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path


DISTRICTS = (
    "창원", "진주", "통영", "사천", "김해", "밀양", "거제", "양산",
    "의령", "함안", "창녕", "고성", "남해", "하동", "산청", "함양",
    "거창", "합천",
)

MANUAL_ALIASES = {
    "김해건설공고등학교": "김해건설공업고등학교",
    "합천교육지원청감계중학교": "감계중학교",
}


def normalize(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s*근무를\s*명함[.]?$", "", text)
    text = text.replace("경상남도", "")
    text = re.sub(r"\[[^]]+]", "", text)
    text = re.sub(r"[\s().·․_\-]", "", text)
    return MANUAL_ALIASES.get(text, text)


def load_personnel(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    prefix = "window.GN_PERSONNEL_DATA="
    if not raw.startswith(prefix) or not raw.endswith(";"):
        raise ValueError(f"Unexpected personnel data wrapper: {path}")
    return json.loads(raw[len(prefix):-1])


def district_from_address(address: str) -> str:
    for district in DISTRICTS:
        if re.search(rf"\b{district}(?:시|군)\b", address or ""):
            return district
    return ""


def district_hints(records: list[dict]) -> set[str]:
    combined = " ".join(
        [str(item.get("to", "")) + " " + str(item.get("from", "")) for item in records]
        + [str(source.get("file", "")) for item in records for source in item.get("sources", [])]
    )
    return {district for district in DISTRICTS if district in combined}


def schoolish(value: str) -> bool:
    return bool(re.search(r"(?:초등학교|중학교|고등학교|학교)(?:[가-힣]*분교장)?(?:\s*근무를\s*명함[.]?)?$", value or ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--school-csv", required=True, type=Path)
    parser.add_argument("--personnel", type=Path, default=Path("tools/personnel-data.js"))
    parser.add_argument("--output", type=Path, default=Path("tools/school-map-data.js"))
    args = parser.parse_args()

    personnel = load_personnel(args.personnel)
    official_schools: list[dict] = []
    with args.school_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("시도교육청명") != "경상남도교육청" or row.get("운영상태") != "운영":
                continue
            address = row.get("소재지도로명주소") or row.get("소재지지번주소") or ""
            district = district_from_address(address)
            if not district or not row.get("위도") or not row.get("경도"):
                continue
            official_schools.append({
                "id": row.get("학교ID", ""),
                "name": row.get("학교명", ""),
                "type": row.get("학교급구분", ""),
                "district": district,
                "address": address,
                "lat": round(float(row["위도"]), 7),
                "lon": round(float(row["경도"]), 7),
            })

    by_name: dict[str, list[dict]] = defaultdict(list)
    for school in official_schools:
        by_name[normalize(school["name"])].append(school)

    histories: dict[str, list[dict]] = defaultdict(list)
    for record in personnel.get("records", []):
        histories[record.get("name", "")].append(record)

    assignments: dict[str, dict[str, dict]] = defaultdict(dict)
    unmatched: set[str] = set()
    ambiguous: set[str] = set()
    for person_name, history in histories.items():
        latest_date = max((item.get("date", "") for item in history), default="")
        latest = [item for item in history if item.get("date") == latest_date]
        hints = district_hints(latest)
        for record in latest:
            workplace = str(record.get("to", ""))
            if not schoolish(workplace):
                continue
            normalized = normalize(workplace)
            candidates = list(by_name.get(normalized, []))
            if not candidates and normalized == normalize("서상중·고등학교"):
                candidates = by_name.get(normalize("서상중학교"), []) + by_name.get(normalize("서상고등학교"), [])
            if not candidates:
                # Regional source labels can be prefixed to the actual school name.
                candidates = [school for key, schools in by_name.items() if normalized.endswith(key) for school in schools]
            if len(candidates) > 1 and hints:
                hinted = [school for school in candidates if school["district"] in hints]
                if hinted:
                    candidates = hinted
            if len(candidates) == 1:
                school = candidates[0]
                assignments[school["id"]][person_name] = {
                    "name": person_name,
                    "role": record.get("toRole") or record.get("fromRole") or "",
                    "date": latest_date,
                }
            elif not candidates:
                unmatched.add(workplace)
            else:
                ambiguous.add(workplace)

    compact_schools = []
    school_by_id = {school["id"]: school for school in official_schools}
    for school_id, people in assignments.items():
        school = dict(school_by_id[school_id])
        school["people"] = sorted(people.values(), key=lambda item: item["name"])
        compact_schools.append(school)
    compact_schools.sort(key=lambda item: (DISTRICTS.index(item["district"]), item["name"]))

    payload = {
        "schemaVersion": 1,
        "source": {
            "title": "전국초중등학교위치표준데이터",
            "url": "https://www.data.go.kr/data/15021148/standard.do",
            "dataDate": "2026-03-20",
        },
        "schools": compact_schools,
        "stats": {
            "officialGyeongnamSchools": len(official_schools),
            "mappedSchools": len(compact_schools),
            "mappedPeople": len({person["name"] for school in compact_schools for person in school["people"]}),
            "unmatchedPlaces": len(unmatched),
            "ambiguousPlaces": len(ambiguous),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "window.GN_SCHOOL_MAP_DATA=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["stats"], ensure_ascii=False))
    if unmatched:
        print("Unmatched:", " | ".join(sorted(unmatched)))
    if ambiguous:
        print("Ambiguous:", " | ".join(sorted(ambiguous)))


if __name__ == "__main__":
    main()
