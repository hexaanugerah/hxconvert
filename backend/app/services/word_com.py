"""DOCX -> PDF through Microsoft Word automation (Windows only).

Word is the reference renderer for Word documents, so exporting through it keeps
the original pagination, fonts, headers/footers, tables, images and floating
objects. Automation runs in a separate short-lived process:

- COM apartment handling stays out of the FastAPI worker threads;
- a hung Word instance can be killed together with its helper process;
- no ``shell=True`` and no user-controlled file names reach a shell.
"""
from __future__ import annotations

from pathlib import Path
import importlib.util
import os
import re
import subprocess
import sys

from docx import Document
from docx.oxml.ns import qn

from app.core.config import BACKEND_DIR, settings
from app.core.errors import ConversionError
from app.services.capabilities import find_winword, word_available

TIMEOUT_SECONDS = 120
_CREATE_NO_WINDOW = 0x08000000


def convert_with_word(input_path: Path, workdir: Path) -> Path:
    """Export a DOCX to PDF using the installed Microsoft Word."""
    if not word_available():
        raise ConversionError("Microsoft Word is not available on this server.")

    output = workdir / f"{input_path.stem}.pdf"
    output.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "app.services.word_com_helper",
        str(input_path.resolve()),
        str(output.resolve()),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process.pid)
        process.communicate()
        raise ConversionError("Microsoft Word export timed out.")
    except OSError as exc:
        raise ConversionError("Microsoft Word could not be started.") from exc

    if process.returncode != 0:
        detail = (stderr or stdout or "").strip().splitlines()
        message = detail[-1][:400] if detail else "unknown error"
        raise ConversionError(f"Microsoft Word failed to export the PDF. {message}")
    if not output.is_file() or output.stat().st_size < 100:
        raise ConversionError("Microsoft Word did not produce a PDF file.")
    with output.open("rb") as handle:
        if not handle.read(5).startswith(b"%PDF"):
            raise ConversionError("Microsoft Word produced an invalid PDF file.")
    return output


def _kill_process_tree(pid: int) -> None:
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:  # pragma: no cover - best effort cleanup
            pass
    else:
        try:
            process = subprocess.Popen(["pkill", "-TERM", "-P", str(pid)])
            process.wait(timeout=10)
        except Exception:  # pragma: no cover
            pass


def _installed_fonts() -> set[str]:
    names: set[str] = set()
    if not sys.platform.startswith("win"):
        return names
    import winreg

    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                index = 0
                while True:
                    try:
                        value_name, value_data, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    for candidate in (value_name, str(value_data)):
                        names.add(_normalise_font(candidate))
        except OSError:
            continue

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if fonts_dir.is_dir():
        for entry in fonts_dir.iterdir():
            if entry.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                names.add(_normalise_font(entry.stem))
    names.discard("")
    return names


def _normalise_font(name: str) -> str:
    value = Path(str(name)).stem.lower()
    value = re.sub(r"\s*\((truetype|opentype|all res|type 1)\)\s*$", "", value)
    value = re.sub(r"\s+(regular|book|roman)$", "", value)
    return value.strip()


def _document_fonts(path: Path) -> set[str]:
    try:
        document = Document(str(path))
    except Exception:
        return set()
    names: set[str] = set()
    for part in document.part.package.parts:
        element = getattr(part, "element", None)
        if element is None:
            continue
        try:
            for run_fonts in element.iter(qn("w:rFonts")):
                for attribute in (
                    "w:ascii",
                    "w:hAnsi",
                    "w:eastAsia",
                    "w:cs",
                ):
                    value = run_fonts.get(qn(attribute))
                    if value and not value.endswith("Theme"):
                        names.add(value.strip())
        except Exception:  # pragma: no cover - malformed package part
            continue
    return {name for name in names if name}


def font_warnings(path: Path) -> list[str]:
    """Report fonts that Word will silently substitute."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import zipfile

        with zipfile.ZipFile(path) as archive:
            if any(name.startswith("word/fonts/") for name in archive.namelist()):
                return []
    except Exception:  # pragma: no cover
        pass
    used = _document_fonts(path)
    if not used:
        return []
    installed = _installed_fonts()
    missing = []
    for font in sorted(used):
        normalised = _normalise_font(font)
        if not normalised:
            continue
        if normalised in installed:
            continue
        if any(name.startswith(f"{normalised} ") for name in installed):
            continue
        missing.append(font)
    if not missing:
        return []
    return [
        "Font berikut tidak tersedia di sistem sehingga Microsoft Word menggantinya: "
        + ", ".join(missing[:10])
        + ". Penggantian font dapat mengubah panjang baris dan jumlah halaman."
    ]
