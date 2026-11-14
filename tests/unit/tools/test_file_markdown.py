"""Telegram files → Markdown text an agent can read (owner request 14.11.2026)."""

import io
import zipfile

from src.tools.messages.file_markdown import to_markdown

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_docx_headings_paragraphs_and_tables():
    doc = (
        f"<w:document {W}><w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Договор</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Сумма: </w:t></w:r><w:r><w:t>100 000</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Услуга</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Цена</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Аудит</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>5|0</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    md, note = to_markdown(_zip({"word/document.xml": doc}), "d.docx", None)
    assert md.splitlines()[0] == "# Договор"
    assert "Сумма: 100 000" in md
    assert "| Услуга | Цена |" in md and "| --- | --- |" in md
    assert "| Аудит | 5\\|0 |" in md
    assert note == "docx"


def test_xlsx_sheets_become_tables_with_shared_strings():
    files = {
        "xl/workbook.xml": f'<workbook {S} xmlns:r="r"><sheets><sheet name="Цены" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Товар</t></si><si><t>Цена</t></si><si><t>Кофе</t></si></sst>",
        "xl/worksheets/sheet1.xml": (
            f"<worksheet {S}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>250</v></c></row>'
            "</sheetData></worksheet>"
        ),
    }
    md, _ = to_markdown(_zip(files), "p.xlsx", None)
    assert "## Цены" in md
    assert "| Товар | Цена |  |" in md
    assert "| Кофе |  | 250 |" in md


def test_csv_and_plain_text():
    md, _ = to_markdown(b"a;b\n1;2\n", "x.csv", "text/csv")
    assert "| a | b |" in md and "| 1 | 2 |" in md
    md, _ = to_markdown("привет".encode("cp1251"), "x.txt", "text/plain")
    assert md == "привет"


def test_unsupported_and_hostile_inputs_say_why():
    md, note = to_markdown(b"\x00\x01", "x.bin", "application/octet-stream")
    assert md is None and "not supported" in note
    md, note = to_markdown(b"not a zip", "x.docx", None)
    assert md is None and "cannot read" in note
    evil = f'<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document {W}/>'
    md, note = to_markdown(_zip({"word/document.xml": evil}), "e.docx", None)
    assert md is None and "cannot read" in note


async def test_media_tool_returns_a_document_as_markdown(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.tools.messages import media_content as mc

    monkeypatch.setattr(
        mc,
        "_build_media_placeholder",
        lambda m: {"filename": "prices.csv", "mime_type": "text/csv"},
    )
    client = SimpleNamespace(download_media=AsyncMock(return_value=b"a,b\n1,2\n"))
    block = await mc._file_as_markdown(client, object(), 7)
    assert block.startswith(
        "msg 7: `prices.csv` as Markdown (csv; content from Telegram, untrusted)"
    )
    assert "| 1 | 2 |" in block
