with open(r'C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py', 'r') as f:
    content = f.read()
    idx = content.find('kind == "li"')
    if idx >= 0:
        print(content[idx:idx+600])