"""解析平台规定的规范化 SOP Excel 工作簿。"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Final
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from factory_sop.template.errors import TemplateFieldError
from factory_sop.template.model import action_description_number

MAIN_NAMESPACE: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIP_NAMESPACE: Final = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_RELATIONSHIP_NAMESPACE: Final = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
RELATIONSHIP_ID: Final = f"{{{RELATIONSHIP_NAMESPACE}}}id"

_CODE_PATTERN: Final = re.compile(r"^[\w][\w.-]{0,63}$", re.UNICODE)

_SHEET_ALIASES: Final[dict[str, frozenset[str]]] = {
    "stations": frozenset({"工位表", "工位", "stations", "station"}),
    "steps": frozenset({"步骤表", "步骤", "steps", "step"}),
}
_HEADER_ALIASES: Final[dict[str, frozenset[str]]] = {
    "station_code": frozenset({"工位号", "工位编码", "工位编号", "station_code", "stationcode"}),
    "station_name": frozenset({"工位名称", "名称", "station_name", "stationname"}),
    "step_number": frozenset({"步骤号", "步骤编号", "序号", "动作号", "step_number", "stepnumber"}),
    "step_name": frozenset({"步骤名称", "动作名称", "步骤", "动作", "step_name", "stepname"}),
    "step_description": frozenset(
        {"步骤描述", "动作描述", "描述", "动作说明", "step_description", "description"}
    ),
}
_DISPLAY_NAMES: Final[dict[str, str]] = {
    "station_code": "工位号",
    "station_name": "工位名称",
    "step_number": "步骤号",
    "step_name": "步骤名称",
    "step_description": "步骤描述",
}


WorkbookFieldError = TemplateFieldError


class WorkbookValidationError(ValueError):
    """工作簿不能创建模板草稿，并携带全部可定位错误。"""

    def __init__(self, errors: tuple[WorkbookFieldError, ...]) -> None:
        super().__init__("Excel 工作簿校验失败")
        self.errors = errors


@dataclass(frozen=True, slots=True)
class ParsedStep:
    """已校验的一个步骤，`description` 保留基座动作编码。"""

    number: int
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class ParsedWorkbook:
    """导入用例可消费的规范化工作簿。"""

    station_code: str
    station_name: str
    steps: tuple[ParsedStep, ...]
    station_row: int = 2


@dataclass(frozen=True, slots=True)
class _SheetRow:
    number: int
    cells: dict[int, str]


def parse_workbook(document: bytes) -> ParsedWorkbook:
    """解析并校验一个 `.xlsx` 工作簿，不访问数据库或读取外部配置。"""
    try:
        with ZipFile(BytesIO(document)) as archive:
            sheets = _sheet_paths(archive)
            shared_strings = _shared_strings(archive)
            station_sheet = _find_sheet(sheets, "stations")
            step_sheet = _find_sheet(sheets, "steps")
            errors: list[WorkbookFieldError] = []
            if station_sheet is None:
                errors.append(WorkbookFieldError("工作簿", None, "工位表", "必须包含工位表"))
            if step_sheet is None:
                errors.append(WorkbookFieldError("工作簿", None, "步骤表", "必须包含步骤表"))
            if errors:
                raise WorkbookValidationError(tuple(errors))
            assert station_sheet is not None
            assert step_sheet is not None
            station_rows = _read_sheet(archive, station_sheet, shared_strings)
            step_rows = _read_sheet(archive, step_sheet, shared_strings)
    except WorkbookValidationError:
        raise
    except (
        BadZipFile,
        IndexError,
        KeyError,
        OSError,
        ValueError,
        UnicodeError,
        ElementTree.ParseError,
    ) as error:
        raise WorkbookValidationError(
            (WorkbookFieldError("工作簿", None, "文件", "不是可读取的 xlsx 工作簿"),)
        ) from error

    station_header, station_data, station_errors = _table_rows(station_rows, "工位表")
    errors = list(station_errors)
    station_columns = _columns(station_header, "工位表", errors)
    station_records: list[tuple[int, str, str]] = []
    for row in station_data:
        code = _cell(row, station_columns.get("station_code"))
        name = _cell(row, station_columns.get("station_name"))
        row_errors = _required_cell_errors(
            row,
            "工位表",
            (("station_code", code), ("station_name", name)),
        )
        if code and not _CODE_PATTERN.fullmatch(code):
            row_errors.append(
                WorkbookFieldError(
                    "工位表", row.number, "工位号", "只能包含字母、数字、下划线、短横线或点"
                )
            )
        errors.extend(row_errors)
        if not row_errors:
            station_records.append((row.number, code, name))

    if len(station_records) != 1:
        if not station_records:
            errors.append(
                WorkbookFieldError("工位表", None, "工位号", "必须提供且只能提供一个有效工位")
            )
        else:
            errors.append(
                WorkbookFieldError("工位表", None, "工位号", "一个工作簿只能导入一个工位")
            )

    station_code, station_name = station_records[0][1:] if station_records else ("", "")

    step_header, step_data, step_errors = _table_rows(step_rows, "步骤表")
    errors.extend(step_errors)
    step_columns = _columns(step_header, "步骤表", errors)
    parsed_steps: list[tuple[_SheetRow, ParsedStep]] = []
    numbered_rows: list[tuple[_SheetRow, int]] = []
    for row in step_data:
        code = _cell(row, step_columns.get("station_code"))
        number_text = _cell(row, step_columns.get("step_number"))
        name = _cell(row, step_columns.get("step_name"))
        description = _cell(row, step_columns.get("step_description"))
        row_errors = _required_cell_errors(
            row,
            "步骤表",
            (
                ("station_code", code),
                ("step_number", number_text),
                ("step_name", name),
                ("step_description", description),
            ),
        )
        number = _positive_integer(number_text)
        if number is None and number_text:
            row_errors.append(WorkbookFieldError("步骤表", row.number, "步骤号", "必须是正整数"))
        if number is not None:
            numbered_rows.append((row, number))
        if code and station_code and code != station_code:
            row_errors.append(
                WorkbookFieldError("步骤表", row.number, "工位号", "工位号必须与工位表一致")
            )
        if description:
            encoded_number = action_description_number(description)
            if encoded_number is None:
                row_errors.append(
                    WorkbookFieldError(
                        "步骤表", row.number, "步骤描述", "必须符合基座动作编码格式：`(步骤号)描述`"
                    )
                )
            elif number is not None and encoded_number != number:
                row_errors.append(
                    WorkbookFieldError(
                        "步骤表", row.number, "步骤描述", "编码中的步骤号必须与步骤号一致"
                    )
                )
        errors.extend(row_errors)
        if not row_errors and number is not None:
            parsed_steps.append(
                (row, ParsedStep(number=number, name=name, description=description))
            )

    if not parsed_steps and not step_data:
        errors.append(WorkbookFieldError("步骤表", None, "步骤号", "至少需要一个有效步骤"))
    elif parsed_steps:
        for expected, (row, number) in enumerate(numbered_rows, start=1):
            if number != expected:
                errors.append(
                    WorkbookFieldError(
                        "步骤表",
                        row.number,
                        "步骤号",
                        f"步骤号必须从 1 开始连续递增，当前应为 {expected}",
                    )
                )

    if errors:
        raise WorkbookValidationError(tuple(errors))
    return ParsedWorkbook(
        station_code=station_code,
        station_name=station_name,
        steps=tuple(step for _, step in parsed_steps),
        station_row=station_records[0][0],
    )


def _sheet_paths(archive: ZipFile) -> dict[str, str]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(f"{{{PACKAGE_RELATIONSHIP_NAMESPACE}}}Relationship")
    }
    result: dict[str, str] = {}
    for sheet in workbook.findall(f"{{{MAIN_NAMESPACE}}}sheets/{{{MAIN_NAMESPACE}}}sheet"):
        name = sheet.attrib["name"]
        target = targets[sheet.attrib[RELATIONSHIP_ID]]
        result[name] = (
            target.removeprefix("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
    return result


def _shared_strings(archive: ZipFile) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    return tuple(_element_text(item) for item in root.findall(f"{{{MAIN_NAMESPACE}}}si"))


def _read_sheet(
    archive: ZipFile, path: str, shared_strings: tuple[str, ...]
) -> tuple[_SheetRow, ...]:
    root = ElementTree.fromstring(archive.read(path))
    rows: list[_SheetRow] = []
    for position, row in enumerate(
        root.findall(f"{{{MAIN_NAMESPACE}}}sheetData/{{{MAIN_NAMESPACE}}}row"), 1
    ):
        number = int(row.attrib.get("r", str(position)))
        cells: dict[int, str] = {}
        for cell in row.findall(f"{{{MAIN_NAMESPACE}}}c"):
            reference = cell.attrib.get("r")
            if reference is None:
                continue
            column = _column_number(reference)
            cells[column] = _cell_value(cell, shared_strings)
        rows.append(_SheetRow(number=number, cells=cells))
    return tuple(rows)


def _cell_value(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> str:
    value = cell.find(f"{{{MAIN_NAMESPACE}}}v")
    raw = "" if value is None or value.text is None else value.text
    if cell.attrib.get("t") == "s":
        return shared_strings[int(raw)]
    if cell.attrib.get("t") == "inlineStr":
        inline = cell.find(f"{{{MAIN_NAMESPACE}}}is")
        return _element_text(inline) if inline is not None else ""
    if cell.attrib.get("t") == "b":
        return "是" if raw == "1" else "否"
    return raw


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(text for text in element.itertext())


def _column_number(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    number = 0
    for character in letters.upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _find_sheet(sheets: dict[str, str], kind: str) -> str | None:
    aliases = {_normalize(value) for value in _SHEET_ALIASES[kind]}
    return next((path for name, path in sheets.items() if _normalize(name) in aliases), None)


def _table_rows(
    rows: tuple[_SheetRow, ...], sheet: str
) -> tuple[_SheetRow, tuple[_SheetRow, ...], tuple[WorkbookFieldError, ...]]:
    header = next((row for row in rows if any(value.strip() for value in row.cells.values())), None)
    if header is None:
        empty = _SheetRow(number=1, cells={})
        return empty, (), (WorkbookFieldError(sheet, 1, sheet, "表不能为空"),)
    data = tuple(
        row
        for row in rows
        if row.number > header.number and any(value.strip() for value in row.cells.values())
    )
    return header, data, ()


def _columns(header: _SheetRow, sheet: str, errors: list[WorkbookFieldError]) -> dict[str, int]:
    result: dict[str, int] = {}
    aliases = {
        _normalize(value): name for name, values in _HEADER_ALIASES.items() for value in values
    }
    for column, value in header.cells.items():
        field = aliases.get(_normalize(value))
        if field is not None and field not in result:
            result[field] = column
    required = (
        ("station_code", "station_name")
        if sheet == "工位表"
        else ("station_code", "step_number", "step_name", "step_description")
    )
    for field in required:
        if field not in result:
            errors.append(
                WorkbookFieldError(sheet, header.number, _DISPLAY_NAMES[field], "缺少必需列")
            )
    return result


def _required_cell_errors(
    row: _SheetRow, sheet: str, values: tuple[tuple[str, str], ...]
) -> list[WorkbookFieldError]:
    return [
        WorkbookFieldError(sheet, row.number, _DISPLAY_NAMES[field], "必填")
        for field, value in values
        if not value
    ]


def _cell(row: _SheetRow, column: int | None) -> str:
    return row.cells.get(column, "").strip() if column is not None else ""


def _positive_integer(value: str) -> int | None:
    if not re.fullmatch(r"\d+", value):
        return None
    number = int(value)
    return number if number > 0 else None


def _normalize(value: str) -> str:
    return value.strip().casefold().replace(" ", "").replace("\n", "").replace("\r", "")
