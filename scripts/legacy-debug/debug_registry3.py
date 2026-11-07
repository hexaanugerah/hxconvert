import re

with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find each ToolSpec block
# ToolSpec starts with "ToolSpec(" and ends with "),"
# We need to handle nested parentheses

# Let's find all ToolSpec boundaries
pattern = r'ToolSpec\([^)]*id="([^"]+)"'
matches = list(re.finditer(pattern, content))

for i, match in enumerate(matches):
    tool_id = match.group(1)
    start = match.start()
    # Find the end of this ToolSpec
    # Look for "),\n" after start
    # But need to handle nested parens
    
    # Simple approach: find next "ToolSpec(" or end of TOOLS tuple
    if i + 1 < len(matches):
        end = matches[i + 1].start()
    else:
        end = content.find(')', start) + 1  # end of TOOLS tuple
    
    tool_block = content[start:end]
    has_max_files = 'max_files=' in tool_block
    print(f"{tool_id}: {'HAS' if has_max_files else 'MISSING'} max_files")