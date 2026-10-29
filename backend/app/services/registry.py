"""Central registry of conversion tools.

Every tool listed here is backed by a real handler. Availability is computed at
runtime so the API never advertises a tool the server cannot execute.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.errors import NotFoundError
from app.services import (
    docx_ops,
    excel_ops,
    html_ops,
    image_ops,
    pdf_ops,
    pdf_to_docx,
    pdf_to_excel,
    pdf_to_pptx,
    pptx_ops,
)
from app.services.base import ConversionContext, ConversionResult
from app.services.capabilities import libreoffice_available, ocr_available

Handler = Callable[[ConversionContext], ConversionResult]

CATEGORIES = [
    {"id": "pdf", "label": "Konversi PDF"},
    {"id": "manage", "label": "Kelola PDF"},
    {"id": "optimize", "label": "Optimasi PDF"},
    {"id": "security", "label": "Keamanan PDF"},
    {"id": "image", "label": "Alat Gambar"},
    {"id": "word", "label": "Word"},
    {"id": "excel", "label": "Excel"},
    {"id": "powerpoint", "label": "PowerPoint"},
    {"id": "text", "label": "Teks & Web"},
]


@dataclass(frozen=True)
class ToolOption:
    name: str
    label: str
    type: str = "text"  # select | text | number | checkbox | pages
    default: Any = None
    choices: tuple[dict[str, Any], ...] = ()
    help: str | None = None
    placeholder: str | None = None
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    id: str
    label: str
    description: str
    category: str
    inputs: tuple[str, ...]
    output: str
    handler: Handler
    output_filename: str = "result"
    multiple: bool = False
    min_files: int = 1
    max_files: int = 1
    requires: str | None = None
    options: tuple[ToolOption, ...] = field(default_factory=tuple)
    processing_note: str | None = None

    def availability(self) -> tuple[bool, str | None]:
        if self.requires == "libreoffice" and not libreoffice_available():
            return False, "LibreOffice is not installed on the server, which this tool requires."
        if self.requires == "ocr" and not ocr_available():
            return False, "No OCR engine is configured on the server (install Tesseract OCR or rapidocr-onnxruntime)."
        return True, None


_LANG_OPTION = ToolOption(
    name="language",
    label="OCR language",
    type="select",
    default="eng",
    choices=(
        {"value": "eng", "label": "English"},
        {"value": "ind", "label": "Bahasa Indonesia"},
        {"value": "eng+ind", "label": "English + Bahasa Indonesia"},
    ),
    help="Language data must be installed for Tesseract; RapidOCR covers Latin scripts.",
)

_PAGES = ToolOption(
    name="pages",
    label="Pages",
    type="text",
    default="all",
    placeholder="e.g. 1,3-5",
    help="Use page numbers separated by commas and ranges, or 'all'.",
)


TOOLS: tuple[ToolSpec, ...] = (
    # ------------------------------- PDF -> others
    ToolSpec(
        id="pdf-to-word",
        label="PDF ke Word",
        description="Konversi PDF menjadi DOCX yang dapat diedit dengan gambar dan tata letak dipertahankan.",
        category="pdf",
        inputs=(".pdf",),
        output="docx",
        output_filename="converted",
        max_files=15,
        handler=pdf_to_docx.convert,
        options=(
            ToolOption(
                name="mode",
                label="Mode konversi",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Otomatis (Cerdas)"},
                    {"value": "layout", "label": "Prioritaskan tata letak"},
                    {"value": "editable", "label": "Prioritaskan pengeditan"},
                    {"value": "full-image", "label": "Gambar penuh (tanpa OCR)"},
                ),
                help=(
                    "Otomatis: halaman scan jadi gambar, halaman teks jadi teks edit, campuran pakai tata letak. "
                    "Tata letak: pdf2docx mempertahankan posisi teks, gambar, grafik vektor. "
                    "Edit: ekstrak teks sebagai paragraf Word. "
                    "Gambar penuh: setiap halaman dirender jadi gambar penuh, tanpa OCR/ekstraksi teks."
                ),
            ),
            ToolOption(
                name="ocr",
                label="Deteksi hasil pindai (OCR)",
                type="select",
                default="never",
                choices=(
                    {"value": "never", "label": "Jangan gunakan OCR"},
                    {"value": "always", "label": "Selalu gunakan OCR"},
                ),
                help="OCR hanya dijalankan saat dipilih 'Selalu gunakan OCR'. Default: tidak ada OCR otomatis.",
            ),
            _LANG_OPTION,
            ToolOption(
                name="page_breaks",
                label="Pertahankan pemisah halaman",
                type="checkbox",
                default=False,
            ),
            ToolOption(
                name="tables",
                label="Pertahankan tabel",
                type="checkbox",
                default=True,
            ),
        ),
        processing_note=(
            "Mode Otomatis: halaman scan tetap gambar, teks digital tetap teks. "
            "Mode Gambar Penuh: semua halaman jadi gambar, tanpa OCR. "
            "OCR tidak dijalankan otomatis."
        ),
    ),
    ToolSpec(
        id="pdf-to-excel",
        label="PDF ke Excel",
        description="Ambil tabel dari PDF dan susun setiap tabel pada lembar kerja terpisah.",
        category="pdf",
        inputs=(".pdf",),
        output="xlsx",
        output_filename="tables",
        max_files=15,
        handler=pdf_to_excel.convert,
        options=(
            ToolOption(
                name="text_fallback",
                label="Ekspor baris teks jika tidak ada tabel",
                type="checkbox",
                default=True,
            ),
        ),
    ),
    ToolSpec(
        id="pdf-to-pptx",
        label="PDF ke PowerPoint",
        description="Ubah setiap halaman PDF menjadi slide yang dapat diedit.",
        category="pdf",
        inputs=(".pdf",),
        output="pptx",
        output_filename="presentation",
        max_files=15,
        handler=pdf_to_pptx.convert,
        options=(
            ToolOption(name="include_images", label="Sertakan gambar halaman", type="checkbox", default=False),
        ),
    ),
    ToolSpec(
        id="pdf-to-images",
        label="PDF ke Gambar",
        description="Render halaman PDF menjadi gambar PNG atau JPG dalam satu arsip ZIP.",
        category="pdf",
        inputs=(".pdf",),
        output="zip",
        output_filename="pages",
        handler=pdf_ops.pdf_to_images,
        max_files=15,
        options=(
            ToolOption(
                name="format",
                label="Format gambar",
                type="select",
                default="png",
                choices=({"value": "png", "label": "PNG"}, {"value": "jpg", "label": "JPG"}),
            ),
            ToolOption(name="dpi", label="Resolusi (DPI)", type="number", default=150, min=72, max=400),
        ),
    ),
    ToolSpec(
        id="image-to-pdf",
        label="Gambar ke PDF",
        description="Gabungkan satu atau beberapa gambar menjadi satu PDF, setiap gambar satu halaman.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"),
        output="pdf",
        output_filename="images",
        handler=image_ops.images_to_pdf,
        max_files=15,
        multiple=True,
        options=(
            ToolOption(
                name="page_size",
                label="Ukuran halaman",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Otomatis (ikuti ukuran gambar)"},
                    {"value": "a4", "label": "A4"},
                    {"value": "letter", "label": "Letter"},
                    {"value": "legal", "label": "Legal"},
                    {"value": "a3", "label": "A3"},
                    {"value": "f4", "label": "F4 / Folio"},
                ),
                help="Mode otomatis mempertahankan rasio dan orientasi setiap gambar.",
            ),
            ToolOption(
                name="fit",
                label="Penempatan gambar",
                type="select",
                default="fit",
                choices=(
                    {"value": "fit", "label": "Sesuaikan ke halaman"},
                    {"value": "fill", "label": "Isi halaman (potong tepi)"},
                    {"value": "original", "label": "Ukuran asli"},
                ),
                help="Semua mode menjaga rasio aspek gambar.",
            ),
            ToolOption(
                name="orientation",
                label="Orientasi",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Otomatis per gambar"},
                    {"value": "portrait", "label": "Tegak"},
                    {"value": "landscape", "label": "Mendatar"},
                ),
                help="Hanya dipakai untuk ukuran kertas standar.",
            ),
            ToolOption(
                name="dpi",
                label="Resolusi (DPI)",
                type="number",
                default=150,
                min=72,
                max=400,
            ),
        ),
    ),
    # ------------------------------- PDF utilities
    ToolSpec(
        id="merge-pdf",
        label="Gabungkan PDF",
        description="Gabungkan beberapa berkas PDF menjadi satu dokumen dengan urutan unggahan.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="merged",
        handler=pdf_ops.merge_pdfs,
        multiple=True,
        min_files=2,
        max_files=15,
    ),
    ToolSpec(
        id="split-pdf",
        label="Pisahkan PDF",
        description="Pecah PDF menjadi berkas per halaman atau per rentang halaman dalam arsip ZIP.",
        category="manage",
        inputs=(".pdf",),
        output="zip",
        output_filename="split",
        max_files=15,
        handler=pdf_ops.split_pdf,
        options=(
            ToolOption(
                name="mode",
                label="Mode pemisahan",
                type="select",
                default="each",
                choices=(
                    {"value": "each", "label": "Satu berkas per halaman"},
                    {"value": "ranges", "label": "Kelompokkan berdasarkan rentang"},
                ),
            ),
            ToolOption(
                name="ranges",
                label="Rentang halaman",
                type="text",
                default="",
                placeholder="1-3, 4-6",
                help="Hanya dipakai pada mode rentang.",
            ),
        ),
    ),
    ToolSpec(
        id="rotate-pdf",
        label="Putar Halaman PDF",
        description="Putar seluruh atau sebagian halaman PDF sebesar 90, 180, atau 270 derajat.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="rotated",
        max_files=15,
        handler=pdf_ops.rotate_pdf,
        options=(
            ToolOption(
                name="angle",
                label="Sudut rotasi",
                type="select",
                default="90",
                choices=(
                    {"value": "90", "label": "90° searah jarum jam"},
                    {"value": "180", "label": "180°"},
                    {"value": "270", "label": "270° searah jarum jam"},
                ),
            ),
            _PAGES,
        ),
    ),
    ToolSpec(
        id="compress-pdf",
        label="Kompres PDF",
        description="Optimalkan struktur berkas, kompresi objek, dan subset font untuk mengecilkan ukuran.",
        category="optimize",
        inputs=(".pdf",),
        output="pdf",
        output_filename="compressed",
        max_files=15,
        handler=pdf_ops.compress_pdf,
    ),
    ToolSpec(
        id="extract-text",
        label="Ekstrak Teks PDF",
        description="Ambil seluruh teks PDF ke berkas TXT dengan penanda halaman.",
        category="pdf",
        inputs=(".pdf",),
        output="txt",
        output_filename="text",
        max_files=15,
        handler=pdf_ops.extract_text,
        options=(_LANG_OPTION,),
    ),
    ToolSpec(
        id="extract-tables",
        label="Ambil Tabel PDF",
        description="Deteksi tabel di PDF dan ekspor ke lembar kerja Excel.",
        category="pdf",
        inputs=(".pdf",),
        output="xlsx",
        output_filename="tables",
        max_files=15,
        handler=pdf_to_excel.convert,
        options=(
            ToolOption(name="text_fallback", label="Ekspor teks jika tidak ada tabel", type="checkbox", default=True),
        ),
    ),
    ToolSpec(
        id="add-page-numbers",
        label="Tambah Nomor Halaman",
        description="Sisipkan nomor halaman pada posisi dan format yang dipilih.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="numbered",
        max_files=15,
        handler=pdf_ops.add_page_numbers,
        options=(
            ToolOption(
                name="position",
                label="Posisi",
                type="select",
                default="bottom-center",
                choices=(
                    {"value": "bottom-center", "label": "Bawah tengah"},
                    {"value": "bottom-right", "label": "Bawah kanan"},
                    {"value": "top-center", "label": "Atas tengah"},
                ),
            ),
            ToolOption(name="start_at", label="Mulai dari nomor", type="number", default=1, min=1, max=9999),
            ToolOption(
                name="format",
                label="Format",
                type="select",
                default="{n}",
                choices=(
                    {"value": "{n}", "label": "1"},
                    {"value": "Page {n}", "label": "Page 1"},
                    {"value": "Halaman {n}", "label": "Halaman 1"},
                    {"value": "{n} / {total}", "label": "1 / 10"},
                ),
            ),
        ),
    ),
    ToolSpec(
        id="add-watermark",
        label="Tambah Tanda Air",
        description="Tambahkan tanda air teks diagonal dengan transparansi yang dapat diatur.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="watermarked",
        max_files=15,
        handler=pdf_ops.add_watermark,
        options=(
            ToolOption(name="text", label="Teks tanda air", type="text", default="CONFIDENTIAL", placeholder="CONFIDENTIAL"),
            ToolOption(name="opacity", label="Transparansi (%)", type="number", default=15, min=5, max=80),
            ToolOption(name="font_size", label="Ukuran huruf", type="number", default=48, min=12, max=120),
        ),
    ),
    ToolSpec(
        id="remove-pages",
        label="Hapus Halaman",
        description="Hapus halaman tertentu dari PDF.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="removed-pages",
        max_files=15,
        handler=pdf_ops.remove_pages,
        options=(
            ToolOption(name="pages", label="Halaman yang dihapus", type="text", default="", placeholder="2,4-6"),
        ),
    ),
    ToolSpec(
        id="reorder-pdf",
        label="Urutkan Halaman PDF",
        description="Susun ulang halaman sesuai urutan yang ditentukan.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="reordered",
        max_files=15,
        handler=pdf_ops.reorder_pdf,
        options=(
            ToolOption(name="order", label="Urutan halaman", type="text", default="", placeholder="3,1,2"),
        ),
    ),
    ToolSpec(
        id="extract-pages",
        label="Ekstrak Halaman PDF",
        description="Ambil halaman tertentu menjadi berkas PDF baru tanpa mengubah dokumen asli.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="extracted-pages",
        max_files=15,
        handler=pdf_ops.extract_pages,
        options=(
            ToolOption(
                name="pages",
                label="Halaman yang diambil",
                type="text",
                default="1",
                placeholder="1,3-5",
                help="Gunakan nomor halaman, rentang, atau 'all'.",
            ),
        ),
    ),
    ToolSpec(
        id="crop-pages",
        label="Potong Halaman PDF",
        description="Potong margin halaman PDF berdasarkan persentase dari setiap sisi.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="cropped",
        max_files=15,
        handler=pdf_ops.crop_pages,
        options=(
            ToolOption(name="top", label="Potong atas (%)", type="number", default=0, min=0, max=45),
            ToolOption(name="right", label="Potong kanan (%)", type="number", default=0, min=0, max=45),
            ToolOption(name="bottom", label="Potong bawah (%)", type="number", default=0, min=0, max=45),
            ToolOption(name="left", label="Potong kiri (%)", type="number", default=0, min=0, max=45),
            _PAGES,
        ),
    ),
    ToolSpec(
        id="resize-pages",
        label="Ubah Ukuran Halaman PDF",
        description="Sesuaikan ukuran halaman ke A4, Letter, Legal, A3, atau F4 tanpa mengubah teks menjadi gambar.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="resized",
        max_files=15,
        handler=pdf_ops.resize_pages,
        options=(
            ToolOption(
                name="page_size",
                label="Ukuran kertas",
                type="select",
                default="a4",
                choices=(
                    {"value": "a4", "label": "A4"},
                    {"value": "letter", "label": "Letter"},
                    {"value": "legal", "label": "Legal"},
                    {"value": "a3", "label": "A3"},
                    {"value": "f4", "label": "F4 / Folio"},
                ),
            ),
            ToolOption(
                name="orientation",
                label="Orientasi",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Ikuti halaman asli"},
                    {"value": "portrait", "label": "Tegak"},
                    {"value": "landscape", "label": "Mendatar"},
                ),
            ),
            ToolOption(
                name="margin_mm",
                label="Margin (mm)",
                type="number",
                default=0,
                min=0,
                max=25,
            ),
        ),
    ),
    ToolSpec(
        id="optimize-pdf",
        label="Optimalkan PDF",
        description="Kompresi ulang gambar, subset font, dan bersihkan objek untuk memperkecil ukuran berkas.",
        category="optimize",
        inputs=(".pdf",),
        output="pdf",
        output_filename="optimized",
        max_files=15,
        handler=pdf_ops.optimize_pdf,
        options=(
            ToolOption(
                name="quality",
                label="Kualitas gambar",
                type="number",
                default=75,
                min=40,
                max=95,
                help="Nilai lebih rendah menghasilkan berkas lebih kecil.",
            ),
            ToolOption(
                name="dpi",
                label="Resolusi gambar target (DPI)",
                type="number",
                default=150,
                min=72,
                max=300,
            ),
        ),
    ),
    ToolSpec(
        id="repair-pdf",
        label="Perbaiki PDF Rusak",
        description="Bangun ulang struktur berkas PDF yang rusak dan pulihkan halaman yang masih dapat dibaca.",
        category="optimize",
        inputs=(".pdf",),
        output="pdf",
        output_filename="repaired",
        max_files=15,
        handler=pdf_ops.repair_pdf,
    ),
    ToolSpec(
        id="protect-pdf",
        label="Lindungi PDF dengan Kata Sandi",
        description="Enkripsi PDF dengan kata sandi AES-256 dan atur izin cetak serta salin.",
        category="security",
        inputs=(".pdf",),
        output="pdf",
        output_filename="protected",
        max_files=15,
        handler=pdf_ops.protect_pdf,
        options=(
            ToolOption(
                name="password",
                label="Kata sandi",
                type="text",
                default="",
                placeholder="Minimal 4 karakter",
                help="Kata sandi tidak dapat dipulihkan jika lupa.",
            ),
            ToolOption(
                name="allow_printing",
                label="Izinkan pencetakan",
                type="checkbox",
                default=True,
            ),
            ToolOption(
                name="allow_copying",
                label="Izinkan penyalinan teks",
                type="checkbox",
                default=False,
            ),
        ),
    ),
    ToolSpec(
        id="unlock-pdf",
        label="Buka Proteksi PDF",
        description="Hapus kata sandi dari PDF yang Anda miliki menggunakan kata sandi yang sah.",
        category="security",
        inputs=(".pdf",),
        output="pdf",
        output_filename="unlocked",
        max_files=15,
        handler=pdf_ops.unlock_pdf,
        options=(
            ToolOption(
                name="password",
                label="Kata sandi saat ini",
                type="text",
                default="",
                help="Hanya gunakan pada dokumen yang Anda berhak membukanya.",
            ),
        ),
        processing_note="Gunakan hanya untuk dokumen milik Anda atau yang Anda berwenang membukanya.",
    ),
    ToolSpec(
        id="add-signature",
        label="Tambah Tanda Tangan",
        description="Sisipkan gambar tanda tangan Anda ke halaman PDF. Ini tanda tangan visual, bukan digital bersertifikat.",
        category="security",
        inputs=(".pdf", ".png", ".jpg", ".jpeg"),
        output="pdf",
        output_filename="signed",
        min_files=2,
        max_files=2,
        multiple=True,
        handler=pdf_ops.add_signature,
        options=(
            ToolOption(
                name="pages",
                label="Halaman tujuan",
                type="text",
                default="last",
                placeholder="all, first, last, 1,3-5",
            ),
            ToolOption(
                name="position",
                label="Posisi tanda tangan",
                type="select",
                default="bottom-right",
                choices=(
                    {"value": "bottom-right", "label": "Bawah kanan"},
                    {"value": "bottom-left", "label": "Bawah kiri"},
                    {"value": "bottom-center", "label": "Bawah tengah"},
                    {"value": "top-right", "label": "Atas kanan"},
                    {"value": "top-left", "label": "Atas kiri"},
                    {"value": "center", "label": "Tengah"},
                ),
            ),
            ToolOption(
                name="width",
                label="Lebar tanda tangan (% halaman)",
                type="number",
                default=25,
                min=5,
                max=60,
            ),
            ToolOption(
                name="remove_background",
                label="Hilangkan latar putih",
                type="checkbox",
                default=True,
                help="Berguna untuk tanda tangan hasil pindai dengan latar putih.",
            ),
        ),
        processing_note="Unggah tepat satu PDF dan satu gambar tanda tangan. Tanda tangan visual tidak diverifikasi secara kriptografis.",
    ),
    ToolSpec(
        id="compare-pdf",
        label="Bandingkan PDF",
        description="Bandingkan dua PDF halaman demi halaman dan buat laporan visual area yang berbeda.",
        category="manage",
        inputs=(".pdf",),
        output="pdf",
        output_filename="comparison-report",
        min_files=2,
        max_files=2,
        multiple=True,
        handler=pdf_ops.compare_pdfs,
        processing_note="Perbandingan dilakukan pada hasil render halaman, bukan perbandingan semantik teks.",
    ),
    # ------------------------------- Word
    ToolSpec(
        id="docx-to-pdf",
        label="Word ke PDF",
        description=(
            "Ubah DOCX menjadi PDF dengan mempertahankan tata letak. Microsoft Word atau "
            "LibreOffice dipakai bila tersedia."
        ),
        category="word",
        inputs=(".docx",),
        output="pdf",
        output_filename="document",
        max_files=15,
        handler=docx_ops.docx_to_pdf,
    ),
    ToolSpec(
        id="docx-to-txt",
        label="Word ke TXT",
        description="Ambil teks dan tabel DOCX ke berkas teks biasa.",
        category="word",
        inputs=(".docx",),
        output="txt",
        output_filename="document",
        max_files=15,
        handler=docx_ops.docx_to_txt,
    ),
    ToolSpec(
        id="docx-to-html",
        label="Word ke HTML",
        description="Konversi DOCX ke HTML dengan gambar tertanam.",
        category="word",
        inputs=(".docx",),
        output="html",
        output_filename="document",
        max_files=15,
        handler=docx_ops.docx_to_html,
    ),
    ToolSpec(
        id="docx-to-markdown",
        label="Word ke Markdown",
        description="Konversi judul, daftar, tabel, dan teks berformat menjadi Markdown.",
        category="word",
        inputs=(".docx",),
        output="md",
        output_filename="document",
        max_files=15,
        handler=docx_ops.docx_to_markdown,
    ),
    ToolSpec(
        id="txt-to-docx",
        label="TXT ke Word",
        description="Buat dokumen Word dari berkas teks dengan pemisahan paragraf.",
        category="word",
        inputs=(".txt",),
        output="docx",
        output_filename="document",
        max_files=15,
        handler=docx_ops.txt_to_docx,
    ),
    ToolSpec(
        id="markdown-to-docx",
        label="Markdown ke Word",
        description="Konversi Markdown (judul, daftar, tabel, tautan) menjadi DOCX.",
        category="word",
        inputs=(".md", ".markdown"),
        output="docx",
        output_filename="document",
        max_files=15,
        handler=docx_ops.markdown_to_docx,
    ),
    ToolSpec(
        id="merge-docx",
        label="Gabungkan Word",
        description="Gabungkan beberapa DOCX beserta gambar dan tabelnya.",
        category="word",
        inputs=(".docx",),
        output="docx",
        output_filename="merged",
        handler=docx_ops.merge_docx,
        multiple=True,
        min_files=2,
        max_files=15,
    ),
    ToolSpec(
        id="clean-docx",
        label="Rapikan Format Word",
        description="Samakan font, ukuran, jarak paragraf, dan hapus paragraf kosong berlebih.",
        category="word",
        inputs=(".docx",),
        output="docx",
        output_filename="cleaned",
        max_files=15,
        handler=docx_ops.clean_docx,
        options=(
            ToolOption(name="font", label="Font isi", type="text", default="Calibri"),
            ToolOption(name="font_size", label="Ukuran huruf", type="number", default=11, min=8, max=24),
        ),
    ),
    # ------------------------------- Excel
    ToolSpec(
        id="xlsx-to-pdf",
        label="Excel ke PDF",
        description="Cetak seluruh lembar kerja ke PDF. LibreOffice memberi hasil paling akurat.",
        category="excel",
        inputs=(".xlsx",),
        output="pdf",
        output_filename="spreadsheet",
        max_files=15,
        handler=excel_ops.xlsx_to_pdf,
    ),
    ToolSpec(
        id="xlsx-to-csv",
        label="Excel ke CSV",
        description="Ekspor setiap lembar kerja ke CSV (satu berkas, atau ZIP untuk banyak lembar).",
        category="excel",
        inputs=(".xlsx",),
        output="csv",
        output_filename="sheet",
        max_files=15,
        handler=excel_ops.xlsx_to_csv,
        options=(
            ToolOption(
                name="delimiter",
                label="Pemisah",
                type="select",
                default=",",
                choices=(
                    {"value": ",", "label": "Koma (,)"},
                    {"value": ";", "label": "Titik koma (;)"},
                    {"value": "\\t", "label": "Tab"},
                ),
            ),
        ),
    ),
    ToolSpec(
        id="csv-to-xlsx",
        label="CSV ke Excel",
        description="Konversi CSV menjadi lembar kerja Excel dengan deteksi tipe angka.",
        category="excel",
        inputs=(".csv",),
        output="xlsx",
        output_filename="spreadsheet",
        max_files=15,
        handler=excel_ops.csv_to_xlsx,
        options=(
            ToolOption(
                name="delimiter",
                label="Pemisah",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Deteksi otomatis"},
                    {"value": ",", "label": "Koma (,)"},
                    {"value": ";", "label": "Titik koma (;)"},
                ),
            ),
            ToolOption(name="sheet_name", label="Nama lembar", type="text", default="Sheet1"),
        ),
    ),
    ToolSpec(
        id="xlsx-to-html",
        label="Excel ke HTML",
        description="Ubah tiap lembar kerja menjadi tabel HTML.",
        category="excel",
        inputs=(".xlsx",),
        output="html",
        output_filename="spreadsheet",
        max_files=15,
        handler=excel_ops.xlsx_to_html,
    ),
    # ------------------------------- PowerPoint
    ToolSpec(
        id="pptx-to-pdf",
        label="PowerPoint ke PDF",
        description="Konversi presentasi PPTX ke PDF melalui LibreOffice.",
        category="powerpoint",
        inputs=(".pptx",),
        output="pdf",
        output_filename="presentation",
        max_files=15,
        handler=pptx_ops.pptx_to_pdf,
        requires="libreoffice",
    ),
    ToolSpec(
        id="pptx-to-images",
        label="PowerPoint ke Gambar",
        description="Render setiap slide menjadi PNG dalam arsip ZIP.",
        category="powerpoint",
        inputs=(".pptx",),
        output="zip",
        output_filename="slides",
        max_files=15,
        handler=pptx_ops.pptx_to_images,
        requires="libreoffice",
        options=(
            ToolOption(name="dpi", label="Resolusi (DPI)", type="number", default=120, min=72, max=300),
        ),
    ),
    # ------------------------------- Image / OCR
    ToolSpec(
        id="convert-image",
        label="Konversi Gambar",
        description="Ubah format gambar antara JPG, PNG, dan WebP dengan kontrol kualitas.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"),
        output="image",
        output_filename="image",
        max_files=15,
        handler=image_ops.convert_image,
        options=(
            ToolOption(
                name="target",
                label="Format target",
                type="select",
                default="png",
                choices=(
                    {"value": "png", "label": "PNG"},
                    {"value": "jpg", "label": "JPG"},
                    {"value": "webp", "label": "WebP"},
                ),
            ),
            ToolOption(name="quality", label="Kualitas (JPG/WebP)", type="number", default=88, min=40, max=100),
        ),
    ),
    ToolSpec(
        id="image-ocr",
        label="Gambar ke Teks (OCR)",
        description="Baca teks dari gambar atau hasil pindai menggunakan OCR.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"),
        output="txt",
        output_filename="ocr-text",
        max_files=15,
        handler=image_ops.image_to_text,
        requires="ocr",
        options=(
            _LANG_OPTION,
            ToolOption(
                name="rotate",
                label="Rotasi sebelum OCR",
                type="select",
                default="0",
                choices=(
                    {"value": "0", "label": "Tanpa rotasi"},
                    {"value": "90", "label": "90°"},
                    {"value": "180", "label": "180°"},
                    {"value": "270", "label": "270°"},
                ),
            ),
            ToolOption(name="grayscale", label="Ubah ke grayscale", type="checkbox", default=True),
        ),
    ),
    ToolSpec(
        id="compress-image",
        label="Kompres Gambar",
        description="Kecilkan ukuran gambar dengan kontrol kualitas dan pilihan format keluaran.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"),
        output="image",
        output_filename="compressed",
        max_files=15,
        handler=image_ops.compress_image,
        options=(
            ToolOption(
                name="quality",
                label="Kualitas",
                type="number",
                default=75,
                min=30,
                max=100,
                help="Berlaku untuk JPG dan WebP; PNG selalu lossless.",
            ),
            ToolOption(
                name="format",
                label="Format keluaran",
                type="select",
                default="auto",
                choices=(
                    {"value": "auto", "label": "Ikuti format asli"},
                    {"value": "jpg", "label": "JPG"},
                    {"value": "png", "label": "PNG"},
                    {"value": "webp", "label": "WebP"},
                ),
            ),
        ),
    ),
    ToolSpec(
        id="resize-image",
        label="Ubah Ukuran Gambar",
        description="Ubah ukuran gambar berdasarkan persentase atau dimensi piksel.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"),
        output="image",
        output_filename="resized",
        max_files=15,
        handler=image_ops.resize_image,
        options=(
            ToolOption(
                name="mode",
                label="Metode",
                type="select",
                default="percent",
                choices=(
                    {"value": "percent", "label": "Persentase"},
                    {"value": "pixels", "label": "Dimensi piksel"},
                ),
            ),
            ToolOption(
                name="percent",
                label="Persentase ukuran (%)",
                type="number",
                default=50,
                min=1,
                max=400,
                help="Dipakai pada metode persentase.",
            ),
            ToolOption(
                name="width",
                label="Lebar (piksel)",
                type="number",
                default=0,
                min=0,
                max=12000,
                help="0 berarti otomatis. Dipakai pada metode piksel.",
            ),
            ToolOption(
                name="height",
                label="Tinggi (piksel)",
                type="number",
                default=0,
                min=0,
                max=12000,
                help="0 berarti otomatis. Dipakai pada metode piksel.",
            ),
            ToolOption(
                name="keep_aspect",
                label="Jaga rasio aspek",
                type="checkbox",
                default=True,
            ),
        ),
    ),
    ToolSpec(
        id="rotate-image",
        label="Putar Gambar",
        description="Putar gambar 90, 180, atau 270 derajat searah jarum jam.",
        category="image",
        inputs=(".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"),
        output="image",
        output_filename="rotated",
        max_files=15,
        handler=image_ops.rotate_image,
        options=(
            ToolOption(
                name="angle",
                label="Sudut rotasi",
                type="select",
                default="90",
                choices=(
                    {"value": "90", "label": "90° searah jarum jam"},
                    {"value": "180", "label": "180°"},
                    {"value": "270", "label": "270° searah jarum jam"},
                ),
            ),
        ),
    ),
    ToolSpec(
        id="pdf-ocr-to-word",
        label="PDF Pindai ke Word (OCR)",
        description="OCR seluruh halaman PDF pindai menjadi DOCX yang dapat diedit.",
        category="pdf",
        inputs=(".pdf",),
        output="docx",
        output_filename="ocr-document",
        max_files=15,
        handler=pdf_to_docx.convert_ocr,
        requires="ocr",
        options=(_LANG_OPTION,),
        processing_note="Setiap halaman diproses dengan OCR sehingga lebih lambat.",
    ),
    # ------------------------------- Text / Web
    ToolSpec(
        id="html-to-pdf",
        label="HTML ke PDF",
        description="Render berkas HTML menjadi PDF.",
        category="text",
        inputs=(".html", ".htm"),
        output="pdf",
        output_filename="document",
        max_files=15,
        handler=html_ops.html_file_to_pdf,
    ),
    ToolSpec(
        id="txt-to-pdf",
        label="TXT ke PDF",
        description="Ubah teks biasa menjadi PDF dengan tipografi yang rapi.",
        category="text",
        inputs=(".txt",),
        output="pdf",
        output_filename="document",
        max_files=15,
        handler=html_ops.text_to_pdf,
    ),
    ToolSpec(
        id="markdown-to-pdf",
        label="Markdown ke PDF",
        description="Render Markdown menjadi PDF.",
        category="text",
        inputs=(".md", ".markdown"),
        output="pdf",
        output_filename="document",
        max_files=15,
        handler=html_ops.markdown_to_pdf,
    ),
)

TOOLS_BY_ID: dict[str, ToolSpec] = {tool.id: tool for tool in TOOLS}


def get_tool(tool_id: str) -> ToolSpec:
    tool = TOOLS_BY_ID.get(tool_id)
    if tool is None:
        raise NotFoundError(f"Unknown conversion tool '{tool_id}'.")
    return tool


def require_available(tool: ToolSpec) -> None:
    available, reason = tool.availability()
    if not available:
        from app.core.errors import UnsupportedFormatError

        raise UnsupportedFormatError(reason or "This tool is not available on this server.")


def tools_public() -> list[dict]:
    result = []
    for tool in TOOLS:
        available, reason = tool.availability()
        result.append(
            {
                "id": tool.id,
                "label": tool.label,
                "description": tool.description,
                "category": tool.category,
                "inputs": list(tool.inputs),
                "output": tool.output,
                "available": available,
                "unavailable_reason": reason,
                "multiple": tool.multiple,
                "min_files": tool.min_files,
                "max_files": tool.max_files,
                "processing_note": tool.processing_note,
                "options": [
                    {
                        "name": option.name,
                        "label": option.label,
                        "type": option.type,
                        "default": option.default,
                        "choices": list(option.choices),
                        "help": option.help,
                        "placeholder": option.placeholder,
                        "min": option.min,
                        "max": option.max,
                    }
                    for option in tool.options
                ],
            }
        )
    return result


def supported_formats() -> dict[str, list[str]]:
    inputs: set[str] = set()
    outputs: set[str] = set()
    for tool in TOOLS:
        available, _ = tool.availability()
        if not available:
            continue
        inputs.update(tool.inputs)
        outputs.add(tool.output)
    return {
        "input": sorted(fmt.lstrip(".") for fmt in inputs if fmt != "image"),
        "output": sorted(fmt for fmt in outputs if fmt != "image") + ["png", "jpg", "webp"],
    }
