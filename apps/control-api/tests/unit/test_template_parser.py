from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from factory_sop.template.parser import (
    ParsedStep,
    ParsedWorkbook,
    WorkbookValidationError,
    parse_workbook,
)


def _xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """构造一个带共享字符串的最小合成 xlsx fixture。"""
    names = list(sheets)
    shared = list(dict.fromkeys(value for rows in sheets.values() for row in rows for value in row))
    shared_index = {value: index for index, value in enumerate(shared)}

    def cell(column: int, row: int, value: str) -> str:
        reference = ""
        current = column
        while current:
            current, remainder = divmod(current - 1, 26)
            reference = chr(65 + remainder) + reference
        return f'<c r="{reference}{row}" t="s"><v>{shared_index[value]}</v></c>'

    def sheet_xml(rows: list[list[str]]) -> str:
        rendered = []
        for row_number, row in enumerate(rows, start=1):
            cells = "".join(cell(column, row_number, value) for column, value in enumerate(row, 1))
            rendered.append(f'<row r="{row_number}">{cells}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rendered)}</sheetData></worksheet>"
        )

    workbook_sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}" />'
        for index, name in enumerate(names, 1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="/xl/worksheets/sheet{index}.xml" />'
            for index in range(1, len(names) + 1)
        )
        + "</Relationships>"
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        for index, name in enumerate(names, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml(sheets[name]))
    return output.getvalue()


def test_platform_workbook_is_parsed_into_a_normalized_template() -> None:
    document = _xlsx(
        {
            "工位表": [["工位号", "工位名称"], ["A-001", "装配一号工位"]],
            "步骤表": [
                ["工位号", "步骤号", "步骤名称", "步骤描述"],
                ["A-001", "1", "取料", "(1)取料"],
                ["A-001", "2", "安装", "(2)安装"],
            ],
        }
    )

    parsed = parse_workbook(document)

    assert parsed == ParsedWorkbook(
        station_code="A-001",
        station_name="装配一号工位",
        steps=(
            ParsedStep(number=1, name="取料", description="(1)取料"),
            ParsedStep(number=2, name="安装", description="(2)安装"),
        ),
    )


def test_parser_reports_every_invalid_cell_with_sheet_row_and_field() -> None:
    document = _xlsx(
        {
            "工位表": [["工位号", "工位名称"], ["A-001", "装配一号工位"]],
            "步骤表": [
                ["工位号", "步骤号", "步骤名称", "步骤描述"],
                ["A-001", "1", "取料", "(1)取料"],
                ["B-999", "3", "安装", "3)缺少左括号"],
            ],
        }
    )

    with pytest.raises(WorkbookValidationError) as raised:
        parse_workbook(document)

    assert [(error.sheet, error.row, error.field) for error in raised.value.errors] == [
        ("步骤表", 3, "工位号"),
        ("步骤表", 3, "步骤描述"),
        ("步骤表", 3, "步骤号"),
    ]
    assert all(error.message for error in raised.value.errors)


def test_parser_requires_both_step_name_and_encoded_description() -> None:
    document = _xlsx(
        {
            "工位表": [["工位号", "工位名称"], ["A-001", "装配一号工位"]],
            "步骤表": [
                ["工位号", "步骤号", "步骤名称", "步骤描述"],
                ["A-001", "1", "", ""],
            ],
        }
    )

    with pytest.raises(WorkbookValidationError) as raised:
        parse_workbook(document)

    assert [(error.row, error.field) for error in raised.value.errors] == [
        (2, "步骤名称"),
        (2, "步骤描述"),
    ]
