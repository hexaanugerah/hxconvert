# Panduan Kontribusi

## Menjalankan pengujian backend

Dari folder `backend/`:

```bash
python -m pytest -q
```

## Kapan dan bagaimana menaikkan `ENGINE_VERSION`

Hasil konversi di-cache dengan kunci yang menyertakan `ENGINE_VERSION`
(`backend/app/services/result_cache.py`). Jika perilaku mesin konversi berubah
tanpa menaikkan konstanta itu, pengguna akan terus menerima hasil cache lama
setelah deploy.

### Kapan wajib menaikkan

Naikkan `ENGINE_VERSION` pada commit yang sama dengan perubahan yang
memengaruhi isi berkas hasil konversi, misalnya:

- kode handler konversi (mis. `app/services/pdf_to_docx.py`, `office_ops.py`,
  `pdf_ops.py`, `pptx_ops.py`) atau helper yang dipakainya;
- nilai default opsi konversi (mode PDF → Word, OCR, kualitas gambar, dan
  sejenisnya);
- versi atau perilaku pustaka konversi (pdf2docx, python-docx, LibreOffice,
  dan lainnya);
- perbaikan fidelitas seperti penulisan `w:tblGrid`, lebar kolom, font, atau
  tata letak halaman.

Tidak perlu menaikkan versi untuk perubahan yang tidak mengubah keluaran,
misalnya refactor tanpa efek, perbaikan pesan galat, atau perubahan UI.

### Cara menaikkan

1. Ubah `ENGINE_VERSION` di `backend/app/services/result_cache.py` dengan pola
   tanggal dan urutan rilis, contoh `"2026-11-25.2"` → `"2026-11-26.1"`.
   Gunakan `.1`, `.2`, dan seterusnya untuk beberapa perubahan pada hari yang
   sama.
2. Perbarui `RECORDED_ENGINE_VERSION` dan `RECORDED_FINGERPRINTS` di
   `backend/tests/test_engine_version.py`. Test ini menangkap perubahan
   keluaran DOCX; bila hash berubah tanpa versi yang dinaikkan, test gagal.
3. Jalankan test terkait dan pastikan lulus:

   ```bash
   python -m pytest -q tests/test_engine_version.py tests/test_layout_fidelity.py
   ```

4. Sebutkan kenaikan versi di pesan commit agar riwayat cache mudah dilacak.
