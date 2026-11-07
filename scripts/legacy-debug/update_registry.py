import re

# Read the registry file
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# First, update merge-pdf from max_files=10 to max_files=15
content = content.replace(
    'max_files=10,',
    'max_files=15,'
)

# List of tool IDs that need max_files=15 added (they don't have it)
tools_needing_max_files = [
    "pdf-to-excel",
    "pdf-to-pptx",
    "split-pdf",
    "rotate-pdf",
    "compress-pdf",
    "extract-text",
    "extract-tables",
    "add-page-numbers",
    "add-watermark",
    "remove-pages",
    "reorder-pdf",
    "docx-to-pdf",
    "docx-to-txt",
    "docx-to-html",
    "docx-to-markdown",
    "txt-to-docx",
    "markdown-to-docx",
    "clean-docx",
    "xlsx-to-pdf",
    "xlsx-to-csv",
    "csv-to-xlsx",
    "xlsx-to-html",
    "pptx-to-pdf",
    "pptx-to-images",
    "convert-image",
    "image-ocr",
    "pdf-ocr-to-word",
    "html-to-pdf",
    "txt-to-pdf",
    "markdown-to-pdf",
]

# For each tool, find its ToolSpec and add max_files=15
for tool_id in tools_needing_max_files:
    # Find the ToolSpec for this tool
    pattern = rf'(ToolSpec\(\s*id="{tool_id}",.*?output_filename="[^"]*",)'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        # Check if max_files already exists in this ToolSpec
        tool_spec = match.group(0)
        if 'max_files=' not in tool_spec:
            # Find where to insert - after output_filename
            new_spec = tool_spec.replace(
                '",\n        handler=',
                '",\n        max_files=15,\n        handler='
            ).replace(
                '",\n        multiple=True,',
                '",\n        max_files=15,\n        multiple=True,'
            ).replace(
                '",\n        requires=',
                '",\n        max_files=15,\n        requires='
            ).replace(
                '",\n        options=',
                '",\n        max_files=15,\n        options='
            )
            content = content.replace(tool_spec, new_spec)
            print(f"Updated {tool_id}")
        else:
            print(f"{tool_id} already has max_files")
    else:
        print(f"Could not find {tool_id}")

# Write back
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done!")