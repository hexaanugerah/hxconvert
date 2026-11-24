"""Tests for the python-docx table-cell cache and the cheap table-grid check."""
from __future__ import annotations

import fitz
from docx import Document

from app.services import pdf_to_docx
from app.services.docx_speedups import install_table_cell_cache


def test_cell_grid_is_cached_but_tracks_structure_changes():
    install_table_cell_cache()
    document = Document()
    table = document.add_table(rows=1, cols=2)

    first_read = table._cells
    assert first_read is table._cells  # cached, not rebuilt on every lookup
    assert len(first_read) == 2

    table.add_row()
    assert len(table._cells) == 4

    merged = table.cell(0, 0).merge(table.cell(0, 1))
    assert merged._tc is table.cell(0, 1)._tc
    assert table.cell(0, 0)._tc.grid_span == 2


def test_rect_drawn_rules_detect_a_table_grid():
    """Form rules are often thin filled rectangles, not stroke lines."""
    document = fitz.open()
    page = document.new_page(width=600, height=400)
    for x in (50, 150, 250, 350, 450):
        page.draw_rect(fitz.Rect(x, 50, x + 0.8, 300), fill=(0, 0, 0))
    for y in (50, 120, 190, 260):
        page.draw_rect(fitz.Rect(50, y, 450, y + 0.8), fill=(0, 0, 0))

    assert (
        pdf_to_docx._has_complex_table(
            page, page.rect.get_area(), 500, page.get_drawings()
        )
        is True
    )
    document.close()
