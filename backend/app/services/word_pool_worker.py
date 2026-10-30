"""Long-lived Microsoft Word COM worker for the persistent Word pool.

Started (and owned) by ``app.services.word_com``. Protocol on stdin/stdout:

- the worker opens one Word instance at start-up and prints ``READY``;
- each request is one JSON line: ``{"id": N, "jobs": [{"input": ..., "output": ...}]}``;
- the reply is one JSON line: ``{"id": N, "results": [{"ok": ..., "error": ...}]}``;
- stdin EOF (or ``"action": "shutdown"``) quits Word and exits gracefully, so
  the pool can stop workers just by closing the pipe.

Word staying warm across requests removes the ~4.5 s instance cost from every
batch conversion.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _main() -> int:  # pragma: no cover - requires a real Word installation
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")

        from word_com_helper import _create_app, _export, _quit

        import pythoncom

        pythoncom.CoInitialize()
    except Exception as exc:
        print(f"FATAL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    app = None
    try:
        app = _create_app()
    except Exception as exc:
        print(f"FATAL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("READY", flush=True)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                continue
            results: list[dict] = []
            for job in request.get("jobs") or []:
                try:
                    input_path = Path(str(job["input"])).resolve()
                    output_path = Path(str(job["output"])).resolve()
                    if not input_path.is_file():
                        raise FileNotFoundError(f"input file not found: {input_path}")
                    output_path.unlink(missing_ok=True)
                    _export(app, input_path, output_path)
                    results.append({"ok": True})
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                    results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]})
            print(json.dumps({"id": request.get("id"), "results": results}), flush=True)
            # Every job failing means Word itself is most likely wedged. Exit so
            # the pool replaces the instance with a fresh one instead of
            # returning the same failure forever.
            if request.get("action") == "shutdown" or (
                results and all(not item.get("ok") for item in results)
            ):
                break
    finally:
        _quit(app)
        pythoncom.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
