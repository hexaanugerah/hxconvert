"""Targeted python-docx speedups used by the layout converter.

``pdf2docx`` fills a table cell by cell and calls ``table.cell(i, j)`` twice per
cell. python-docx resolves that through ``Table._cells``, which rebuilds the
whole cell grid (all ``w:tc`` proxies, grid spans and vertical merges) on every
single call: O(rows x cols) work per cell, i.e. O((rows x cols)^2) per table.
For a 13x8 form that is ~1.3 s of pure proxy construction per page.

The cache below stores the grid on the ``Table`` instance that pdf2docx keeps
for the whole table build, and is dropped whenever that instance changes the
structure (adding rows/columns, or merging cells). Instances are cheap wrappers
created on each attribute access, so unrelated code never sees a stale grid it
did not build itself.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_installed = False
_CACHE_ATTR = "_docuconvert_cells"

_original_cells_descriptor = None
_original_merge = None
_original_add_row = None
_original_add_column = None


def _invalidate(table) -> None:
    try:
        table.__dict__.pop(_CACHE_ATTR, None)
    except AttributeError:  # pragma: no cover - defensive
        pass


def install_table_cell_cache() -> None:
    """Cache ``Table._cells`` per table instance (idempotent, process-wide)."""
    global _installed, _original_cells_descriptor
    global _original_merge, _original_add_row, _original_add_column
    if _installed:
        return
    with _lock:
        if _installed:
            return
        from docx.table import Table, _Cell

        _original_cells_descriptor = Table.__dict__["_cells"]
        original_get_cells = _original_cells_descriptor.fget

        def _cells(self):
            cached = self.__dict__.get(_CACHE_ATTR)
            if cached is not None:
                return cached
            cells = original_get_cells(self)
            self.__dict__[_CACHE_ATTR] = cells
            return cells

        _original_merge = _Cell.merge
        _original_add_row = Table.add_row
        _original_add_column = Table.add_column

        def merge(self, other_cell):
            _invalidate(self._parent)
            return _original_merge(self, other_cell)

        def add_row(self, *args, **kwargs):
            _invalidate(self)
            return _original_add_row(self, *args, **kwargs)

        def add_column(self, *args, **kwargs):
            _invalidate(self)
            return _original_add_column(self, *args, **kwargs)

        Table._cells = property(_cells)
        _Cell.merge = merge
        Table.add_row = add_row
        Table.add_column = add_column
        _installed = True
