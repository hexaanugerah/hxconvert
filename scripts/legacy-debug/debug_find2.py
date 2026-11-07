with open(r'C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\pdf_to_docx.py', 'r') as f:
    content = f.read()
    idx = content.find('_add_text_block')
    if idx >= 0:
        # Find the second occurrence (the function definition)
        idx2 = content.find('_add_text_block', idx+1)
        if idx2 >= 0:
            print(content[idx2:idx2+1000])