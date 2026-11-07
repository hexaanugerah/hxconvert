import re

with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix the regex group - should be group(3) for content
old_li = '''    if kind == "li":
        match = BULLET_RE.match(text) or NUMBER_RE.match(text)
        content = match.group(2) if match else text
        style = "List Number" if NUMBER_RE.match(text) else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        # Add content as plain text - runs include list marker which we don't want
        _add_runs(paragraph, [(content, False, False, body_size, False)])
        return'''

new_li = '''    if kind == "li":
        match = BULLET_RE.match(text) or NUMBER_RE.match(text)
        content = match.group(3) if match else text
        style = "List Number" if NUMBER_RE.match(text) else "List Bullet"
        paragraph = document.add_paragraph(style=style)
        # Add content as plain text - runs include list marker which we don't want
        _add_runs(paragraph, [(content, False, False, body_size, False)])
        return'''

if old_li in content:
    content = content.replace(old_li, new_li)
    with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("Fixed regex group")
else:
    print("Could not find the pattern to replace")