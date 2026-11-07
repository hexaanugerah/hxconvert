import re

with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# First, update merge-pdf from max_files=10 to max_files=15
content = content.replace(
    'max_files=10,',
    'max_files=15,'
)

# Find each ToolSpec block and add max_files where missing
pattern = r'ToolSpec\([^)]*id="([^"]+)"'
matches = list(re.finditer(pattern, content))

# Process in reverse order so positions don't shift
for i in range(len(matches) - 1, -1, -1):
    match = matches[i]
    tool_id = match.group(1)
    start = match.start()
    
    # Find end of this ToolSpec
    if i + 1 < len(matches):
        end = matches[i + 1].start()
    else:
        # Find the closing of TOOLS tuple
        end = content.find('),\n)', start)
        if end == -1:
            end = len(content)
    
    tool_block = content[start:end]
    
    if 'max_files=' not in tool_block:
        # Find output_filename in this block
        output_match = re.search(r'output_filename="[^"]*",', tool_block)
        if output_match:
            # Insert max_files=15, after output_filename
            insert_pos = start + output_match.end()
            # Get indentation
            line_start = content.rfind('\n', 0, insert_pos) + 1
            indent = len(content[line_start:insert_pos]) - len(content[line_start:insert_pos].lstrip())
            indent_str = content[line_start:insert_pos][:indent]
            content = content[:insert_pos] + '\n' + indent_str + 'max_files=15,' + content[insert_pos:]
            print(f"Added max_files=15 to {tool_id}")
        else:
            print(f"Could not find output_filename for {tool_id}")
    else:
        print(f"{tool_id} already has max_files")

# Write back
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done!")