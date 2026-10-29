"""Excel tools: XLSX -> PDF/CSV/HTML and CSV -> XLSX."""
from __future__ import annotations

import csv
from datetime import date, datetime
from html import escape
from io import StringIO
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core.errors import ConversionError, ValidationAppError
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import libreoffice_available
from app.services.html_ops import PDF_MIME, render_html_to_pdf, wrap_html
from app.services.office_ops import convert_with_soffice

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_SHEETS = 30
MAX_ROWS = 5000
MAX_COLUMNS = 60
COLUMNS_PER_BLOCK = 10

# openpyxl paper size codes -> (width, height) in inches, portrait.
PAPER_SIZES = {
    1: (8.5, 11.0),    # Letter
    5: (8.5, 14.0),    # Legal
    8: (11.69, 16.54),  # A3
    9: (8.27, 11.69),  # A4
    14: (8.27, 13.0),  # F4 / Folio
}
_INT_RE = re.compile(r"^-?\d{1,15}$")
_FLOAT_RE = re.compile(r"^-?\d{1,15}(\.\d+)?$")


def _open_workbook(path: Path):
    try:
        return load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:
        raise ConversionError("The Excel file could not be opened. It may be damaged.") from exc


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _parse_number(text: str):
    stripped = text.strip()
    if not stripped:
        return text
    if _INT_RE.match(stripped):
        if len(stripped.lstrip("-")) > 1 and stripped.lstrip("-").startswith("0"):
            return text
        return int(stripped)
    if _FLOAT_RE.match(stripped) and "." in stripped:
        try:
            return float(stripped)
        except ValueError:
            return text
    return text


def _sheet_title(name: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "-", str(name)).strip()[:31] or "Sheet"


def _print_settings(path: Path) -> list[tuple[str, int | None]]:
    """Read orientation/paper size straight from the sheet XML.

    openpyxl's read-only worksheets do not expose ``page_setup``, and re-opening
    the whole workbook in normal mode would defeat read-only memory savings.
    """
    import xml.etree.ElementTree as ET

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    settings: list[tuple[str, int | None]] = []
    try:
        with ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                rel.get("Id"): rel.get("Target", "")
                for rel in rels.findall(f"{{{pkg_ns}}}Relationship")
            }
            for sheet in workbook.findall(f".//{{{main_ns}}}sheet"):
                target = targets.get(sheet.get(f"{{{rel_ns}}}id"), "")
                target = target.lstrip("/")
                if not target:
                    settings.append(("portrait", None))
                    continue
                if not target.startswith("xl/"):
                    target = f"xl/{target}"
                sheet_xml = ET.fromstring(archive.read(target))
                setup = sheet_xml.find(f".//{{{main_ns}}}pageSetup")
                if setup is None:
                    settings.append(("portrait", None))
                    continue
                orientation = (setup.get("orientation") or "portrait").lower()
                paper = setup.get("paperSize")
                try:
                    paper_size = int(paper) if paper else None
                except ValueError:
                    paper_size = None
                settings.append((orientation, paper_size))
    except Exception:
        return []
    return settings


def xlsx_to_pdf(ctx: ConversionContext) -> ConversionResult:
    workbook = _open_workbook(ctx.input)
    workbook.close()
    stem = Path(ctx.original_name).stem or "spreadsheet"
    if libreoffice_available():
        output = convert_with_soffice(ctx.input, "pdf", ctx.workdir)
        return ConversionResult(output, f"{stem}.pdf", PDF_MIME)

    workbook = _open_workbook(ctx.input)
    sections: list[str] = []
    warnings = [
        "LibreOffice is not installed, so sheets were printed as simplified HTML tables. "
        "Charts, images, merged cells and exact print areas are not reproduced."
    ]
    truncated = False
    split_sheets: list[str] = []
    orientations: set[str] = set()
    paper_sizes: set[int] = set()
    print_settings = _print_settings(ctx.input)
    try:
        for index, sheet in enumerate(workbook.worksheets[:MAX_SHEETS]):
            if index:
                sections.append("<div style='page-break-before: always;'></div>")

            orientation, paper_size = (
                print_settings[index] if index < len(print_settings) else ("portrait", None)
            )
            orientations.add(orientation)
            if paper_size in PAPER_SIZES:
                paper_sizes.add(int(paper_size))

            sections.append(f"<h2>{escape(sheet.title)}</h2>")
            used_columns = min(max(sheet.max_column or 1, 1), MAX_COLUMNS)
            rows: list[list[str]] = []
            for row in sheet.iter_rows(max_row=MAX_ROWS, max_col=used_columns):
                values = [_cell_text(cell.value) for cell in row]
                while values and not values[-1]:
                    values.pop()
                if any(values):
                    rows.append(values)
            if not rows:
                sections.append("<p><i>Empty sheet</i></p>")
            else:
                width = max(len(values) for values in rows)
                if width > COLUMNS_PER_BLOCK:
                    split_sheets.append(sheet.title)
                compact_class = " class='compact'" if width > 8 else ""
                for start in range(0, width, COLUMNS_PER_BLOCK):
                    end = min(width, start + COLUMNS_PER_BLOCK)
                    if start:
                        sections.append("<div style='page-break-before: always;'></div>")
                        sections.append(f"<h3>{escape(sheet.title)} (kolom {start + 1}-{end})</h3>")
                    rows_html: list[str] = []
                    for row_index, values in enumerate(rows):
                        block = values[start:end]
                        if not block:
                            continue
                        tag = "th" if row_index == 0 else "td"
                        cells = "".join(f"<{tag}>{escape(value)}</{tag}>" for value in block)
                        rows_html.append(f"<tr>{cells}</tr>")
                    sections.append(f"<table{compact_class}>" + "".join(rows_html) + "</table>")
            if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                truncated = True
    finally:
        workbook.close()

    if not sections:
        raise ConversionError("The workbook contains no sheets.")
    if truncated:
        warnings.append(
            f"Only the first {MAX_ROWS} rows and {MAX_COLUMNS} columns of each sheet were printed."
        )
    if split_sheets:
        warnings.append(
            "Wide sheets were split into side-by-side column blocks instead of cutting off the "
            "right side: " + ", ".join(split_sheets[:6]) + "."
        )

    landscape = "landscape" in orientations
    if landscape:
        paper = next((size for size in paper_sizes if size in PAPER_SIZES), 9)
        width, height = PAPER_SIZES[paper]
        if width < height:
            width, height = height, width
        if len(orientations) > 1:
            warnings.append(
                "Lembar kerja memakai orientasi berbeda; semua dicetak mendatar agar tabel lebar "
                "tidak terpotong."
            )
        page_css = f"@page {{ size: {width:.2f}in {height:.2f}in; margin: 10mm; }}"
    else:
        paper = next((size for size in paper_sizes if size in PAPER_SIZES), 9)
        width, height = PAPER_SIZES[paper]
        page_css = f"@page {{ size: {width:.2f}in {height:.2f}in; margin: 10mm; }}"
    page_css += " table.compact th, table.compact td { font-size: 7.5pt; padding: 2pt 3pt; }"

    output = render_html_to_pdf(
        wrap_html("".join(sections), stem, page_css=page_css),
        ctx.workdir / "spreadsheet.pdf",
    )
    return ConversionResult(output, f"{stem}.pdf", PDF_MIME, warnings)


def xlsx_to_csv(ctx: ConversionContext) -> ConversionResult:
    delimiter = str(ctx.options.get("delimiter", ","))
    if delimiter in ("\\t", "\t", "tab"):
        delimiter = "\t"
    if delimiter not in (",", ";"):
        raise ValidationAppError("Delimiter must be a comma, semicolon or tab.")

    workbook = _open_workbook(ctx.input)
    try:
        sheets = workbook.worksheets
        if not sheets:
            raise ConversionError("The workbook contains no sheets.")
        if len(sheets) > MAX_SHEETS:
            raise ConversionError(f"The workbook has more than {MAX_SHEETS} sheets, which exceeds the limit.")

        def write_sheet(sheet, destination) -> None:
            with destination.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=delimiter)
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else _cell_text(value) for value in row]
                    if not any(values):
                        continue
                    writer.writerow(values)

        if len(sheets) == 1:
            output = ctx.workdir / "sheet.csv"
            write_sheet(sheets[0], output)
            filename = f"{Path(ctx.original_name).stem or 'sheet'}.csv"
            return ConversionResult(output, filename, "text/csv; charset=utf-8")

        zip_path = ctx.workdir / "sheets.zip"
        with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
            for sheet in sheets:
                buffer = StringIO()
                writer = csv.writer(buffer, delimiter=delimiter)
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else _cell_text(value) for value in row]
                    if not any(values):
                        continue
                    writer.writerow(values)
                archive.writestr(f"{_sheet_title(sheet.title)}.csv", buffer.getvalue().encode("utf-8-sig"))
        return ConversionResult(
            zip_path,
            f"{Path(ctx.original_name).stem or 'workbook'}-csv.zip",
            "application/zip",
            ["The workbook has several sheets, so each sheet was exported as a separate CSV in a ZIP archive."],
        )
    finally:
        workbook.close()


def csv_to_xlsx(ctx: ConversionContext) -> ConversionResult:
    raw = ctx.input.read_bytes()
    if not raw.strip():
        raise ValidationAppError("The CSV file is empty.")
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ConversionError("The CSV file encoding could not be detected.")

    requested = str(ctx.options.get("delimiter", "auto"))
    delimiter = None
    if requested == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    else:
        delimiter = "\t" if requested in ("\\t", "\t", "tab") else requested

    rows = list(csv.reader(StringIO(text), delimiter=delimiter))
    if not rows:
        raise ValidationAppError("The CSV file contains no rows.")
    columns = max(len(row) for row in rows)
    if columns > 400:
        raise ConversionError("The CSV has more than 400 columns, which exceeds the limit.")

    sheet_name = re.sub(r"[\[\]:*?/\\]", "-", str(ctx.options.get("sheet_name") or "Sheet1")).strip()[:31] or "Sheet1"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    widths: dict[int, int] = {}
    for row_index, row in enumerate(rows[:20000], start=1):
        for column_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=column_index, value=_parse_number(value))
            widths[column_index] = max(widths.get(column_index, 8), min(50, len(value) + 2))
    for column_index, width in widths.items():
        sheet.column_dimensions[get_column_letter(column_index)].width = width
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E8F0FE")
    sheet.freeze_panes = "A2"

    output = ctx.workdir / "spreadsheet.xlsx"
    workbook.save(output)
    return ConversionResult(output, f"{Path(ctx.original_name).stem or 'spreadsheet'}.xlsx", XLSX_MIME)


def xlsx_to_html(ctx: ConversionContext) -> ConversionResult:
    workbook = _open_workbook(ctx.input)
    sections: list[str] = []
    warnings: list[str] = []
    try:
        for sheet in workbook.worksheets[:MAX_SHEETS]:
            sections.append(f"<h2>{escape(sheet.title)}</h2>")
            rows_html = []
            used_columns = min(sheet.max_column or 1, MAX_COLUMNS)
            for row_index, row in enumerate(sheet.iter_rows(max_row=MAX_ROWS, max_col=used_columns)):
                values = [_cell_text(cell.value) for cell in row]
                while values and not values[-1]:
                    values.pop()
                if not any(values):
                    continue
                tag = "th" if row_index == 0 else "td"
                rows_html.append("<tr>" + "".join(f"<{tag}>{escape(v)}</{tag}>" for v in values) + "</tr>")
            sections.append("<table>" + "".join(rows_html) + "</table>" if rows_html else "<p><i>Empty sheet</i></p>")
            if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                warnings.append(f"Sheet '{sheet.title}' was truncated to {MAX_ROWS} rows and {MAX_COLUMNS} columns.")
    finally:
        workbook.close()
    html = wrap_html("".join(sections), Path(ctx.original_name).stem)
    output = ctx.workdir / "spreadsheet.html"
    output.write_text(html, encoding="utf-8")
    return ConversionResult(
        output,
        f"{Path(ctx.original_name).stem or 'spreadsheet'}.html",
        "text/html; charset=utf-8",
        warnings,
    )
