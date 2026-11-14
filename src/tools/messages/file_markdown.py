"""A Telegram file as Markdown text, so an agent that cannot open files can read it.

Standard library only: the deployed image carries no document libraries, and
docx/xlsx are ZIP archives of XML. PDF needs a real parser and is not converted here
(get_media_content still returns it as a file). Pictures are not recognised in a
request (AGENTS.md: recognition belongs to the background pipeline); their stored
text comes from the archive instead.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
import zipfile

MAX_MEMBER_BYTES = 50 * 1024 * 1024  # uncompressed, per XML part: no zip bombs
MAX_ROWS_PER_SHEET = 500
TEXT_TYPES = (".txt", ".md", ".markdown", ".log")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class _UnreadableError(ValueError):
    pass


def _cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(_cell(c) for c in rows[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise _UnreadableError(f"{name} is too large")
    data = z.read(name)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise _UnreadableError("XML with a DTD is refused")
    return ET.fromstring(data)


def _docx(z: zipfile.ZipFile) -> str:
    body = _xml(z, "word/document.xml").find(f"{_W}body")
    out: list[str] = []
    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            text = "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()
            style = el.find(f"{_W}pPr/{_W}pStyle")
            level = (style.get(f"{_W}val") or "") if style is not None else ""
            if text and level.lower().startswith("heading") and level[-1:].isdigit():
                text = "#" * min(int(level[-1]), 6) + " " + text
            if text:
                out.append(text)
        elif el.tag == f"{_W}tbl":
            rows = [
                [
                    "".join(t.text or "" for t in tc.iter(f"{_W}t"))
                    for tc in tr.iter(f"{_W}tc")
                ]
                for tr in el.iter(f"{_W}tr")
            ]
            out.append(_table(rows))
    return "\n\n".join(out)


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _xlsx(z: zipfile.ZipFile) -> str:
    names = set(z.namelist())
    shared = (
        [
            "".join(t.text or "" for t in si.iter(f"{_S}t"))
            for si in _xml(z, "xl/sharedStrings.xml")
        ]
        if "xl/sharedStrings.xml" in names
        else []
    )
    sheets = _xml(z, "xl/workbook.xml").iter(f"{_S}sheet")
    out: list[str] = []
    for i, sheet in enumerate(sheets, start=1):
        part = f"xl/worksheets/sheet{i}.xml"
        if part not in names:
            continue
        rows: list[list[str]] = []
        for row in _xml(z, part).iter(f"{_S}row"):
            if len(rows) >= MAX_ROWS_PER_SHEET:
                break
            cells: dict[int, str] = {}
            for c in row.iter(f"{_S}c"):
                v = c.find(f"{_S}v")
                value = (
                    v.text
                    if v is not None
                    else "".join(t.text or "" for t in c.iter(f"{_S}t"))
                )
                if c.get("t") == "s" and value is not None:
                    value = shared[int(value)]
                cells[_col(c.get("r") or "A")] = value or ""
            rows.append([cells.get(k, "") for k in range(max(cells, default=-1) + 1)])
        out.append(f"## {sheet.get('name')}\n\n{_table(rows)}")
    return "\n\n".join(out)


def to_markdown(
    raw: bytes, filename: str | None, mime: str | None
) -> tuple[str | None, str]:
    """(markdown, format) or (None, why not). Never raises on a bad file."""
    name = (filename or "").lower()
    mime = (mime or "").lower()
    try:
        if name.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                return _docx(z), "docx"
        if name.endswith(".xlsx"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                return _xlsx(z), "xlsx"
        if name.endswith(".csv") or mime == "text/csv":
            text = _decode(raw)
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
            return _table(list(csv.reader(io.StringIO(text), dialect))), "csv"
        if name.endswith(".json") or mime == "application/json":
            return "```json\n" + json.dumps(
                json.loads(_decode(raw)), ensure_ascii=False, indent=2
            ) + "\n```", "json"
        if name.endswith(TEXT_TYPES) or mime.startswith("text/"):
            return _decode(raw), "text"
    except (
        zipfile.BadZipFile,
        KeyError,
        ET.ParseError,
        _UnreadableError,
        csv.Error,
        ValueError,
        IndexError,
    ) as e:
        return (
            None,
            f"cannot read this file as {name.rsplit('.', 1)[-1] or mime}: {type(e).__name__}: {e}",
        )
    return (
        None,
        f"format not supported for Markdown ({filename or mime or 'unknown'}): docx, xlsx, csv, json, txt, md are",
    )
