from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import pdfplumber


RELEVANT_ROLES = {"수학교사", "교감", "교장", "장학사", "장학관", "기관장"}
OCR_NAME_FIXES = {"박그T현": "박유현"}
DATE_RE = re.compile(r"(20\d{2})\s*[.년/-]\s*(3|9)\s*[.월/-]\s*1")
ARROW_RE = re.compile(r"([^\n▶▣]{1,34}?)\s*(?:→|->)\s*([^\n▶▣]{1,34})")
SPACE_RE = re.compile(r"\s+")


def clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\u200b", " ")
    return SPACE_RE.sub(" ", text).strip(" \t|·․")


def compact(value: object) -> str:
    return re.sub(r"[\s\n\r_.·․ㆍ:：()\[\]-]", "", clean(value))


def effective_date(path: Path) -> str | None:
    for value in (path.name, path.parent.name, str(path)):
        match = DATE_RE.search(value)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-01"
    return None


def role_from_text(value: str, *, prefer_specific: bool = True) -> str:
    text = compact(value)
    if not text:
        return ""
    if "수학" in text and ("교사" in text or len(text) <= 8):
        return "수학교사"
    if any(token in text for token in ("교육장", "직속기관장", "기관장", "미래교육국장", "학교정책국장")):
        return "기관장"
    if "장학관" in text or "교육연구관" in text:
        return "장학관"
    if "장학사" in text or "교육연구사" in text:
        return "장학사"
    if "교장" in text:
        return "교장"
    if "교감" in text:
        return "교감"
    if prefer_specific and any(token in text for token in ("본청과장", "직속기관부장", "과장및부장")):
        return "장학관"
    if "국장" in text:
        return "기관장"
    if "교사" in text or "교원" in text:
        return "교사"
    return ""


def extract_transition(context: str, fallback_role: str = "") -> tuple[str, str, str]:
    context = context.replace("놔", "→")
    lines = [clean(line) for line in context.splitlines() if clean(line)]
    transition = ""
    from_role = ""
    to_role = ""

    for line in reversed(lines[-12:]):
        if "→" not in line and "->" not in line:
            continue
        match = ARROW_RE.search(line)
        if not match:
            continue
        left, right = clean(match.group(1)), clean(match.group(2))
        candidate_from = role_from_text(left)
        candidate_to = role_from_text(right)
        if candidate_from or candidate_to:
            transition = clean(line)
            from_role, to_role = candidate_from, candidate_to
            break

    if not to_role:
        for line in reversed(lines[-10:]):
            role = role_from_text(line)
            if role in RELEVANT_ROLES:
                to_role = role
                transition = transition or clean(line)
                break

    if not to_role:
        to_role = fallback_role
    if not from_role and to_role:
        from_role = to_role
    return from_role, to_role, transition


def header_map(rows: Sequence[Sequence[object]]) -> tuple[int, dict[str, int], list[str]] | None:
    # 한글 인사표는 "임용사항" 아래에 신임지/현임지를 둔 2단 머리글이 많다.
    # 첫 행부터 최대 4행까지 열별 텍스트를 합쳐 단일 머리글처럼 해석한다.
    column_count = max((len(row) for row in rows[:5]), default=0)
    for header_index in range(min(5, len(rows))):
        headers = []
        for column in range(column_count):
            parts = []
            for row_index in range(header_index + 1):
                row = rows[row_index]
                if column < len(row) and clean(row[column]):
                    parts.append(clean(row[column]))
            headers.append(compact(" ".join(parts)))
        if not any("성명" in cell or "이름" in cell for cell in headers):
            continue
        mapping: dict[str, int] = {}
        for index, cell in enumerate(headers):
            if not cell:
                continue
            if "성명" in cell or cell == "이름":
                mapping.setdefault("name", index)
            if any(token in cell for token in ("신임지", "신임교", "발령지", "전입지", "새근무지")):
                mapping.setdefault("to", index)
            if any(token in cell for token in ("현임지", "현임교", "전임지", "전근무지", "현근무지")):
                mapping.setdefault("from", index)
            if cell in {"소속", "소속교", "학교명"}:
                mapping.setdefault("affiliation", index)
            if any(token in cell for token in ("과목명", "교과명")) or cell in {"과목", "교과"}:
                mapping.setdefault("subject", index)
            if any(token in cell for token in ("직위", "직급", "직명")):
                mapping.setdefault("position", index)
            if "비고" in cell:
                mapping.setdefault("note", index)
        if "name" in mapping and ("to" in mapping or "from" in mapping or "affiliation" in mapping):
            return header_index, mapping, headers
    return None


def looks_like_person(name: str) -> bool:
    value = clean(name)
    if not value or len(value) > 18 or any(ch.isdigit() for ch in value):
        return False
    if any(token in value for token in ("성명", "합계", "계", "구분", "비고", "소계", "신규", "미발령", "미정")):
        return False
    return bool(re.fullmatch(r"[가-힣]{2,6}", value))


def normalize_name(name: str) -> str:
    value = clean(name)
    value = OCR_NAME_FIXES.get(value, value)
    joined = value.replace(" ", "")
    if 2 <= len(joined) <= 6 and re.fullmatch(r"[가-힣]+", joined):
        return joined
    return value


@dataclass
class SourceRef:
    file: str
    page: int | None


def records_from_table(
    rows: Sequence[Sequence[object]],
    context: str,
    source: SourceRef,
    date: str,
) -> list[dict]:
    mapped = header_map(rows)
    if not mapped:
        return []
    header_index, columns, _headers = mapped
    records: list[dict] = []

    for raw_row in rows[header_index + 1 :]:
        row = [clean(cell) for cell in raw_row]
        if not row:
            continue

        def at(key: str) -> str:
            index = columns.get(key)
            return row[index] if index is not None and index < len(row) else ""

        name = normalize_name(at("name"))
        if not looks_like_person(name):
            continue
        subject = at("subject")
        position = at("position")
        to_place = at("to")
        from_place = at("from")
        affiliation = at("affiliation")
        note = at("note")

        # Retirement and similar regional tables often use one affiliation column.
        if affiliation and not from_place and not to_place:
            from_place = affiliation
        elif affiliation and not from_place:
            from_place = affiliation

        is_math = "수학" in compact(subject)
        # 과목 열이 있는 교사 표에서는 수학만 범위에 포함한다. 이전 페이지의
        # 관리자 문맥이 이어지는 경우 다른 교과가 잘못 포함되는 것을 막는다.
        if "subject" in columns and not is_math:
            continue
        explicit_role = role_from_text(position)
        from_role, to_role, transition = extract_transition(context, explicit_role)

        if is_math:
            from_role = to_role = "수학교사"
            transition = "수학교사 인사"
        elif explicit_role in RELEVANT_ROLES:
            # 직위 열이 있는 표는 행 자체의 직위가 페이지 문맥보다 정확하다.
            from_role = to_role = explicit_role
        elif to_role not in RELEVANT_ROLES and from_role not in RELEVANT_ROLES:
            continue

        if not (to_place or from_place):
            continue
        if ("퇴직" in compact(context[-300:]) or "퇴직" in compact(note)) and not to_place:
            to_place = "퇴직"
            to_role = to_role or explicit_role
            transition = "퇴직"

        records.append(
            {
                "date": date,
                "name": name,
                "subject": "수학" if is_math else subject,
                "from": from_place,
                "to": to_place,
                "fromRole": from_role,
                "toRole": to_role,
                "transition": transition,
                "note": note,
                "sources": [{"file": source.file, "page": source.page}],
            }
        )
    return records


def parse_pdf(path: Path, date: str, display_name: str) -> tuple[list[dict], dict]:
    records: list[dict] = []
    table_count = 0
    last_context: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            try:
                tables = page.find_tables()
            except Exception:
                tables = []
            previous_bottom = 0.0
            for table in tables:
                table_count += 1
                top = max(0.0, float(table.bbox[1]))
                if top > previous_bottom:
                    try:
                        segment = page.crop((0, previous_bottom, page.width, top)).extract_text() or ""
                    except Exception:
                        segment = ""
                    for line in segment.splitlines():
                        line = clean(line)
                        if line and not re.match(r"^[-–—]?\s*\d+\s*[-–—]?$", line):
                            last_context.append(line)
                    last_context = last_context[-18:]
                context = "\n".join(last_context)
                try:
                    rows = table.extract()
                except Exception:
                    rows = []
                records.extend(
                    records_from_table(
                        rows,
                        context,
                        SourceRef(display_name, page_number),
                        date,
                    )
                )
                previous_bottom = max(previous_bottom, float(table.bbox[3]))

            if tables:
                try:
                    tail = page.crop((0, previous_bottom, page.width, page.height)).extract_text() or ""
                except Exception:
                    tail = ""
            else:
                try:
                    tail = page.extract_text() or ""
                except Exception:
                    tail = ""
            for line in tail.splitlines():
                line = clean(line)
                if line and not re.match(r"^[-–—]?\s*\d+\s*[-–—]?$", line):
                    last_context.append(line)
            last_context = last_context[-18:]
    return records, {"pages": len(pdf.pages), "tables": table_count}


def _ocr_entries(image_path: Path, ocr_script: Path) -> list[dict]:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ocr_script),
            "-ImagePath",
            str(image_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8-sig",
        errors="replace",
    )
    payload = json.loads(completed.stdout)
    entries: list[dict] = []
    for line in payload.get("lines", []):
        words = line.get("words") or []
        if not words:
            continue
        left = min(float(word["x"]) for word in words)
        top = min(float(word["y"]) for word in words)
        right = max(float(word["x"]) + float(word["w"]) for word in words)
        bottom = max(float(word["y"]) + float(word["h"]) for word in words)
        entries.append(
            {
                "text": clean(line.get("text", "")),
                "x": (left + right) / 2,
                "y": (top + bottom) / 2,
                "left": left,
                "right": right,
            }
        )
    return entries


def _cluster_by_y(entries: list[dict], tolerance: float = 14.0) -> list[list[dict]]:
    clusters: list[list[dict]] = []
    for entry in sorted(entries, key=lambda item: (item["y"], item["x"])):
        if not clusters:
            clusters.append([entry])
            continue
        center = sum(item["y"] for item in clusters[-1]) / len(clusters[-1])
        if abs(entry["y"] - center) <= tolerance:
            clusters[-1].append(entry)
        else:
            clusters.append([entry])
    return clusters


def _ocr_table_records(entries: list[dict], context_seed: list[str], date: str, source: SourceRef) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    ordered = sorted(entries, key=lambda item: (item["y"], item["x"]))
    name_headers = [
        entry
        for entry in ordered
        if compact(entry["text"]) in {"성명", "이름", "성명성명"}
    ]
    detected: list[tuple[float, dict[str, float]]] = []
    for name_header in name_headers:
        same_band = [item for item in ordered if abs(item["y"] - name_header["y"]) <= 24]
        columns: dict[str, float] = {"name": name_header["x"]}
        for item in same_band:
            token = compact(item["text"])
            if any(value in token for value in ("신임지", "신임교", "발령지")):
                columns["to"] = item["x"]
            elif any(value in token for value in ("현임지", "전임지", "현임교", "전근무지")):
                columns["from"] = item["x"]
            elif token in {"과목", "과목명", "교과", "교과명"}:
                columns["subject"] = item["x"]
            elif token in {"직위", "직급", "직명"}:
                columns["position"] = item["x"]
            elif "비고" in token:
                columns["note"] = item["x"]
            elif token in {"소속", "소속교", "학교명"}:
                columns["affiliation"] = item["x"]
        if "to" in columns or "from" in columns or "affiliation" in columns:
            detected.append((name_header["y"], columns))

    for index, (header_y, columns) in enumerate(detected):
        next_y = detected[index + 1][0] if index + 1 < len(detected) else float("inf")
        # 새 소제목이 나오기 전까지를 현재 표로 본다. 일반적으로 행 간격보다
        # 큰 150px 이상의 빈 공간은 다음 표/설명 시작을 의미한다.
        candidates = [item for item in ordered if header_y + 18 < item["y"] < next_y - 10]
        header_names = list(columns)
        header_row = {
            "name": "성명",
            "to": "신임지",
            "from": "현임지",
            "subject": "과목",
            "position": "직위",
            "note": "비고",
            "affiliation": "소속",
        }
        rows: list[list[str]] = [[header_row[key] for key in header_names]]
        column_order = sorted(columns.items(), key=lambda item: item[1])
        centers = [center for _key, center in column_order]
        boundaries = [-(10**9)] + [(centers[i] + centers[i + 1]) / 2 for i in range(len(centers) - 1)] + [10**9]

        def assigned_column(item: dict) -> str | None:
            for col_index, (key, _center) in enumerate(column_order):
                if boundaries[col_index] <= item["x"] < boundaries[col_index + 1]:
                    return key
            return None

        # 여러 줄 셀은 같은 y 좌표에 놓이지 않는다. 성명 셀의 중심을 행 기준으로
        # 삼고, 인접 성명 사이의 세로 범위를 한 행으로 묶는다.
        name_items = [
            item
            for item in candidates
            if assigned_column(item) == "name" and looks_like_person(normalize_name(item["text"]))
        ]
        name_items.sort(key=lambda item: item["y"])
        for row_index, name_item in enumerate(name_items):
            previous_y = name_items[row_index - 1]["y"] if row_index else header_y
            following_y = name_items[row_index + 1]["y"] if row_index + 1 < len(name_items) else min(next_y, name_item["y"] + 150)
            top_bound = (previous_y + name_item["y"]) / 2
            bottom_bound = (name_item["y"] + following_y) / 2
            cluster = [item for item in candidates if top_bound <= item["y"] < bottom_bound]
            values = {key: [] for key in header_names}
            for item in sorted(cluster, key=lambda value: (value["y"], value["x"])):
                assigned = assigned_column(item)
                if assigned:
                    token = clean(item["text"])
                    if token and not token.isdigit():
                        values[assigned].append(token)
            row = [clean(" ".join(values[key])) for key in header_names]
            if any(row):
                rows.append(row)

        preceding = [item for item in ordered if header_y - 700 < item["y"] < header_y - 20]
        context = context_seed + [clean(item["text"]) for item in preceding]
        records.extend(records_from_table(rows, "\n".join(context[-24:]), source, date))

    trailing = [clean(item["text"]) for item in ordered[-20:] if clean(item["text"])]
    return records, (context_seed + trailing)[-24:]


def parse_pdf_ocr(
    path: Path,
    date: str,
    display_name: str,
    cache_root: Path,
    ocr_script: Path,
    pdftoppm: Path,
) -> tuple[list[dict], dict]:
    cache = cache_root / (path.stem + "-ocr")
    cache.mkdir(parents=True, exist_ok=True)
    prefix = cache / "page"
    images = sorted(cache.glob("page-*.png"))
    if not images or max(image.stat().st_mtime for image in images) < path.stat().st_mtime:
        for image in images:
            image.unlink()
        subprocess.run(
            [str(pdftoppm), "-png", "-r", "200", str(path), str(prefix)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        images = sorted(cache.glob("page-*.png"), key=lambda image: int(re.search(r"(\d+)$", image.stem).group(1)))

    records: list[dict] = []
    context: list[str] = []
    for page_number, image in enumerate(images, 1):
        json_cache = image.with_suffix(".ocr.json")
        if json_cache.exists() and json_cache.stat().st_mtime >= image.stat().st_mtime:
            entries = json.loads(json_cache.read_text(encoding="utf-8"))
        else:
            entries = _ocr_entries(image.resolve(), ocr_script.resolve())
            json_cache.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        page_records, context = _ocr_table_records(
            entries,
            context,
            date,
            SourceRef(display_name, page_number),
        )
        records.extend(page_records)
    return records, {"pages": len(images), "tables": None, "ocr": True}


def ensure_hwp_html(path: Path, cache_root: Path, pyhwp_root: Path) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    output = cache_root / (path.stem + ".html")
    index = output / "index.xhtml"
    if index.exists() and index.stat().st_mtime >= path.stat().st_mtime:
        return index
    if output.exists():
        shutil.rmtree(output)
    executable = pyhwp_root / "bin" / "hwp5html.exe"
    if not executable.exists():
        raise RuntimeError("pyhwp가 설치되어 있지 않습니다.")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pyhwp_root)
    subprocess.run(
        [str(executable), "--output", str(output), str(path)],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return index


def parse_hwp(path: Path, date: str, display_name: str, cache_root: Path, pyhwp_root: Path) -> tuple[list[dict], dict]:
    from lxml import etree

    index = ensure_hwp_html(path, cache_root, pyhwp_root)
    tree = etree.parse(str(index))
    root = tree.getroot()
    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    records: list[dict] = []
    context_lines: list[str] = []
    table_count = 0

    for element in root.iter():
        tag = etree.QName(element).localname
        if tag == "p":
            if any(etree.QName(parent).localname in {"td", "th", "table"} for parent in element.iterancestors()):
                continue
            value = clean("".join(element.itertext()))
            if value:
                context_lines.append(value)
                context_lines = context_lines[-18:]
        elif tag == "table":
            if any(etree.QName(parent).localname == "table" for parent in element.iterancestors()):
                continue
            table_count += 1
            rows: list[list[str]] = []
            for tr in element.xpath(".//x:tr", namespaces=namespace):
                row = [clean("".join(cell.itertext())) for cell in tr.xpath("./x:td|./x:th", namespaces=namespace)]
                rows.append(row)
            records.extend(
                records_from_table(
                    rows,
                    "\n".join(context_lines),
                    SourceRef(display_name, None),
                    date,
                )
            )
    return records, {"pages": None, "tables": table_count}


def source_display(path: Path, source_root: Path) -> str:
    try:
        return str(path.relative_to(source_root)).replace("\\", "/")
    except ValueError:
        return path.name


def deduplicate(records: Iterable[dict]) -> list[dict]:
    def canonical_place(value: str) -> str:
        raw = clean(value)
        raw = re.sub(r"^(.+?)\s*\(중\)$", r"\1교육지원청", raw)
        raw = re.sub(r"\[[^\]]+\]", "", raw)
        raw = raw.replace("경상남도", "")
        if "교육지원청" in raw:
            raw = raw.split("교육지원청", 1)[0] + "교육지원청"
        return compact(raw)

    def place_score(value: str) -> tuple[int, int]:
        text = clean(value)
        return (1 if any(token in text for token in ("과", "부", "팀")) else 0, len(compact(text)))

    merged: dict[tuple, dict] = {}
    for record in records:
        key = (
            record["date"],
            compact(record["name"]),
            canonical_place(record["from"]),
            canonical_place(record["to"]),
            record["toRole"],
            compact(record["subject"]),
        )
        if key not in merged:
            merged[key] = record
            continue
        existing = merged[key]
        seen = {(item["file"], item.get("page")) for item in existing["sources"]}
        existing["sources"].extend(
            item for item in record["sources"] if (item["file"], item.get("page")) not in seen
        )
        if place_score(record.get("from", "")) > place_score(existing.get("from", "")):
            existing["from"] = record["from"]
        if place_score(record.get("to", "")) > place_score(existing.get("to", "")):
            existing["to"] = record["to"]
        if "→" in record.get("transition", "") and "→" not in existing.get("transition", ""):
            existing["transition"] = record["transition"]
            existing["fromRole"] = record["fromRole"] or existing["fromRole"]
        if not existing.get("note") and record.get("note"):
            existing["note"] = record["note"]

    result = list(merged.values())
    role_order = {"기관장": 0, "장학관": 1, "교장": 2, "교감": 3, "장학사": 4, "수학교사": 5, "교사": 6, "": 7}
    result.sort(key=lambda item: (item["date"], role_order.get(item["toRole"], 8), item["name"], item["to"]))
    for index, record in enumerate(result, 1):
        record["id"] = f"r{index:05d}"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="경남 중등 인사 자료를 웹툴 데이터로 변환합니다.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("tmp/personnel-hwp"))
    parser.add_argument("--pyhwp", type=Path, default=Path(".codex_work/pyhwp"))
    parser.add_argument("--ocr-script", type=Path, default=Path("scripts/windows-ocr.ps1"))
    parser.add_argument(
        "--pdftoppm",
        type=Path,
        default=Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe",
    )
    args = parser.parse_args()

    source_root = args.source.resolve()
    paths = sorted(
        [*source_root.rglob("*.pdf"), *source_root.rglob("*.hwp")],
        key=lambda item: (effective_date(item) or "", str(item)),
    )
    all_records: list[dict] = []
    catalog: list[dict] = []
    failures: list[dict] = []

    for index, path in enumerate(paths, 1):
        date = effective_date(path)
        display = source_display(path, source_root)
        if not date:
            failures.append({"file": display, "error": "기준일을 파일명 또는 폴더명에서 찾지 못함"})
            continue
        try:
            if path.suffix.lower() == ".pdf":
                with pdfplumber.open(path) as probe:
                    has_text = any(
                        clean(page.extract_text() or "")
                        for page in probe.pages[: min(3, len(probe.pages))]
                    )
                if has_text:
                    records, stats = parse_pdf(path, date, display)
                else:
                    records, stats = parse_pdf_ocr(
                        path,
                        date,
                        display,
                        args.cache,
                        args.ocr_script,
                        args.pdftoppm,
                    )
            else:
                records, stats = parse_hwp(path, date, display, args.cache, args.pyhwp.resolve())
            all_records.extend(records)
            catalog.append(
                {
                    "file": display,
                    "date": date,
                    "type": path.suffix.lower().lstrip("."),
                    "records": len(records),
                    **stats,
                }
            )
            print(f"[{index:02d}/{len(paths):02d}] {date} {display}: {len(records)}건", flush=True)
        except Exception as exc:
            failures.append({"file": display, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[{index:02d}/{len(paths):02d}] 실패 {display}: {exc}", file=sys.stderr, flush=True)

    records = deduplicate(all_records)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "경상남도교육청 중등 인사자료 중 수학교사·교감·교장·장학사·장학관·기관장",
        "records": records,
        "sources": catalog,
        "failures": failures,
        "stats": {
            "records": len(records),
            "people": len({record["name"] for record in records}),
            "sources": len(catalog),
            "dates": sorted({record["date"] for record in records}),
            "roles": dict(Counter(record["toRole"] for record in records)),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    args.output.write_text("window.GN_PERSONNEL_DATA=" + encoded + ";\n", encoding="utf-8")
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    if failures:
        print("실패 파일:", json.dumps(failures, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
