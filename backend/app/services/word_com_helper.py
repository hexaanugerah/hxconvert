"""Isolated Microsoft Word COM exporter.

Run as ``python -m app.services.word_com_helper <input.docx> <output.pdf>``.
It lives in its own process so a stuck Word instance can be killed and COM
apartment state never leaks into the API worker threads.
"""
from __future__ import annotations

import sys
from pathlib import Path

WD_EXPORT_FORMAT_PDF = 17
WD_EXPORT_OPTIMIZE_FOR_PRINT = 1
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: word_com_helper <input.docx> <output.pdf>", file=sys.stderr)
        return 2
    input_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    if not input_path.is_file():
        print(f"input file not found: {input_path}", file=sys.stderr)
        return 3

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    document = None
    try:
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        try:
            app.Options.WarnBeforeSavingPrintingSendingMarkup = False
            app.Options.UpdateLinksAtOpen = False
        except Exception:
            pass

        document = app.Documents.Open(
            FileName=str(input_path),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            OpenAndRepair=False,
            NoEncodingDialog=True,
        )
        document.ExportAsFixedFormat(
            OutputFileName=str(output_path),
            ExportFormat=WD_EXPORT_FORMAT_PDF,
            OpenAfterExport=False,
            OptimizeFor=WD_EXPORT_OPTIMIZE_FOR_PRINT,
            CreateBookmarks=0,
            DocStructureTags=True,
            BitmapMissingFonts=True,
            UseISO19005_1=False,
        )
        return 0
    except Exception as exc:  # pragma: no cover - depends on local Word
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if document is not None:
            try:
                document.Close(SaveChanges=False)
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
