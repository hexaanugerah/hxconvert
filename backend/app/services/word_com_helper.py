"""Isolated Microsoft Word COM exporter.

Two modes:

``python word_com_helper.py <input.docx> <output.pdf>``
    Export one document.

``python word_com_helper.py --batch <manifest.json> <results.json>``
    Export a list of documents through a single Word instance. Batch jobs pay
    Word's startup and shutdown cost once instead of once per file.

It lives in its own process so a stuck Word instance can be killed and COM
apartment state never leaks into the API worker threads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WD_EXPORT_FORMAT_PDF = 17
WD_EXPORT_OPTIMIZE_FOR_PRINT = 1
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3


def _create_app():
    import win32com.client

    app = win32com.client.DispatchEx("Word.Application")
    app.Visible = False
    app.DisplayAlerts = 0
    app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
    try:
        app.Options.WarnBeforeSavingPrintingSendingMarkup = False
        app.Options.UpdateLinksAtOpen = False
    except Exception:
        pass
    return app


def _export(app, input_path: Path, output_path: Path) -> None:
    document = app.Documents.Open(
        FileName=str(input_path),
        ConfirmConversions=False,
        ReadOnly=True,
        AddToRecentFiles=False,
        Visible=False,
        OpenAndRepair=False,
        NoEncodingDialog=True,
    )
    try:
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
    finally:
        try:
            document.Close(SaveChanges=False)
        except Exception:
            pass


def _quit(app) -> None:
    if app is None:
        return
    try:
        # Plain Quit() waits for Word's teardown (~2.7 s per file in practice).
        # The documents are read-only and already closed, so SaveChanges=0 lets
        # the process exit immediately.
        app.Quit(SaveChanges=0)
    except Exception:
        try:
            app.Quit()
        except Exception:
            pass


def _run_single(input_path: Path, output_path: Path) -> int:
    import pythoncom

    pythoncom.CoInitialize()
    app = None
    try:
        app = _create_app()
        _export(app, input_path, output_path)
        return 0
    except Exception as exc:  # pragma: no cover - depends on local Word
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        _quit(app)
        pythoncom.CoUninitialize()


def _run_batch(manifest_path: Path, results_path: Path) -> int:
    import pythoncom

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        jobs = payload.get("jobs") or []
    except Exception as exc:
        print(f"invalid manifest: {exc}", file=sys.stderr)
        results_path.write_text(
            json.dumps({"results": [], "error": f"invalid manifest: {exc}"}),
            encoding="utf-8",
        )
        return 2

    results: list[dict] = []
    pythoncom.CoInitialize()
    app = None
    try:
        app = _create_app()
        for job in jobs:
            try:
                input_path = Path(str(job["input"])).resolve()
                output_path = Path(str(job["output"])).resolve()
                if not input_path.is_file():
                    raise FileNotFoundError(f"input file not found: {input_path}")
                output_path.unlink(missing_ok=True)
                _export(app, input_path, output_path)
                results.append({"ok": True})
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                results.append(
                    {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
                )
    except Exception as exc:  # pragma: no cover - Word unavailable/blocked
        message = f"{type(exc).__name__}: {exc}"[:400]
        while len(results) < len(jobs):
            results.append({"ok": False, "error": message})
        print(message, file=sys.stderr)
    finally:
        _quit(app)
        pythoncom.CoUninitialize()
        results_path.write_text(json.dumps({"results": results}), encoding="utf-8")

    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--batch":
        if len(argv) != 4:
            print(
                "usage: word_com_helper --batch <manifest.json> <results.json>",
                file=sys.stderr,
            )
            return 2
        return _run_batch(Path(argv[2]), Path(argv[3]))

    if len(argv) != 3:
        print("usage: word_com_helper <input.docx> <output.pdf>", file=sys.stderr)
        return 2
    input_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    if not input_path.is_file():
        print(f"input file not found: {input_path}", file=sys.stderr)
        return 3
    output_path.unlink(missing_ok=True)
    return _run_single(input_path, output_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
