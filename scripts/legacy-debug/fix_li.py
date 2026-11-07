import re

with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix the _add_text_block li case - replace the markerless_runs logic with simpler approach
old_li = '''    if kind == "li":
        match = BULLET_RE.match(text) or NUMBER_RE.match(text)
        content = match.group(2) if match else text
        style = "List Number" if NUMBER_RE.match(text) else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        markerless_runs = []
        removed = len(text) - len(content)
        consumed = 0
        for text_part, b, i, size, sup in runs:
            if consumed >= removed:
                markerless_runs.append((text_part, b, i, size, sup))
            consumed += len(text_part)
        _add_runs(paragraph, markerless_runs or [(content, False, False, body_size, False)])
        return'''

new_li = '''    if kind == "li":
        match = BULLET_RE.match(text) or NUMBER_RE.match(text)
        content = match.group(2) if match else text
        style = "List Number" if NUMBER_RE.match(text) else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        # Add content as plain text - runs include list marker which we don't want
        _add_runs(paragraph, [(content, False, False, body_size, False)])
        return'''

if old_li in content:
    content = content.replace(old_li, new_li)
    with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("Fixed _add_text_block li case")
else:
    print("Could not find the pattern to replace")