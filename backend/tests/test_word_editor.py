"""Word editor text replacement must not delete pictures or tabs."""
from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches

from app.api.v1.word import _is_encrypted_docx, _paragraphs, _set_paragraph_text
from tests.conftest import make_png


def test_password_protected_docx_is_reported_not_called_corrupt():
    container = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    assert not _is_encrypted_docx(container)
    assert not _is_encrypted_docx(b"PK\x03\x04rest of a normal docx")
    encrypted = container + "EncryptedPackage".encode("utf-16-le")
    assert _is_encrypted_docx(encrypted)


def _protection(tag: str, edit: str | None):
    node = OxmlElement(tag)
    if edit is not None:
        node.set(qn("w:edit"), edit)
        node.set(qn("w:enforcement"), "1")
    return node


def _picture_run(paragraph):
    return paragraph.add_run().add_picture(BytesIO(make_png()), width=Inches(1))


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    relationship_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def test_editing_an_image_only_paragraph_keeps_the_picture():
    document = Document()
    paragraph = document.add_paragraph()
    _picture_run(paragraph)

    _set_paragraph_text(paragraph, "Caption logo")

    assert paragraph.text == "Caption logo"
    assert len(document.inline_shapes) == 1
    assert "<w:drawing" in paragraph._p.xml


def test_editing_text_around_an_inline_picture_keeps_the_picture():
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Logo: ")
    _picture_run(paragraph)
    paragraph.add_run(" end")

    _set_paragraph_text(paragraph, "Logo: updated end")

    assert paragraph.text == "Logo: updated end"
    assert len(document.inline_shapes) == 1
    assert "<w:drawing" in paragraph._p.xml


def test_editing_keeps_tabs_and_line_breaks():
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("No. Bukti \t: HO/B.\nSecond line")

    _set_paragraph_text(paragraph, "No. Bukti \t: HO/B. 123\nSecond line")

    assert paragraph.text == "No. Bukti \t: HO/B. 123\nSecond line"
    assert "<w:tab" in paragraph._p.xml
    assert "<w:br" in paragraph._p.xml


def test_saved_document_is_no_longer_read_only(client):
    document = Document()
    document.add_paragraph("Formulir")
    document.settings.element.append(_protection("w:documentProtection", "readOnly"))
    document.settings.element.append(_protection("w:writeProtection", None))
    source = BytesIO()
    document.save(source)

    saved = client.post(
        "/api/v1/word/save",
        files={
            "file": (
                "form.docx",
                source.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={
            "changes": '[{"index": 0, "text": "Formulir yang sudah diisi"}]',
            "additions": "[]",
        },
    )
    assert saved.status_code == 200

    result = Document(BytesIO(saved.content))
    assert _paragraphs(result)[0].text == "Formulir yang sudah diisi"
    settings = result.settings.element
    assert not settings.findall(qn("w:documentProtection"))
    assert not settings.findall(qn("w:writeProtection"))


def test_save_endpoint_keeps_inline_picture(client):
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Logo: ")
    _picture_run(paragraph)
    paragraph.add_run(" end")
    document.add_paragraph("Body text")
    source = BytesIO()
    document.save(source)

    inspected = client.post(
        "/api/v1/word/inspect",
        files={
            "file": (
                "logo.docx",
                source.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert inspected.status_code == 200
    paragraphs = inspected.json()["paragraphs"]
    assert paragraphs[0]["text"] == "Logo:  end"

    saved = client.post(
        "/api/v1/word/save",
        files={
            "file": (
                "logo.docx",
                source.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={
            "changes": '[{"index": 0, "text": "Logo: updated end"}]',
            "additions": "[]",
        },
    )
    assert saved.status_code == 200

    result = Document(BytesIO(saved.content))
    assert len(result.inline_shapes) == 1
    assert _paragraphs(result)[0].text == "Logo: updated end"
