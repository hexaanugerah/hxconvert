import re

# Read the registry file
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# First, update merge-pdf from max_files=10 to max_files=15
content = content.replace(
    'max_files=10,',
    'max_files=15,'
)

# The ToolSpec structure is:
# ToolSpec(
#     id="...",
#     ...
#     output_filename="...",
#     handler=...,
#     options=(...),
# ),
# 
# We need to add max_files=15, after output_filename for tools that don't have it

# Let's use a regex that matches each complete ToolSpec
# ToolSpec ends with `),` at the top level (not nested)

# Split by '),\n    ToolSpec(' or '),\n)'
parts = re.split(r'(\)),\n    (ToolSpec\()', content)

# This is complex. Let me try a different approach - process line by line with proper parsing
lines = content.split('\n')
new_lines = []
i = 0

while i < len(lines):
    line = lines[i]
    new_lines.append(line)
    
    # Check if this line starts a ToolSpec
    if 'ToolSpec(' in line and 'id="' in line:
        # Find the end of this ToolSpec
        # Count braces: ToolSpec( ... ), 
        brace_count = 1  # for ToolSpec(
        j = i + 1
        tool_spec_lines = [line]
        has_max_files = False
        output_filename_line = -1
        
        while j < len(lines) and brace_count > 0:
            l = lines[j]
            tool_spec_lines.append(l)
            
            if 'max_files=' in l:
                has_max_files = True
            if 'output_filename=' in l:
                output_filename_line = len(tool_spec_lines) - 1
            
            # Count parentheses
            brace_count += l.count('(') - l.count(')')
            
            # Also count brackets for options tuple
            # But we only care about top-level ToolSpec closing
            if brace_count <= 0:
                break
            j += 1
        
        # Now j is at the closing line
        if not has_max_files and output_filename_line >= 0:
            # Insert max_files=15 after output_filename
            indent = len(lines[i + output_filename_line]) - len(lines[i + output_filename_line].lstrip())
            # We need to insert in new_lines
            # The output_filename line is already in new_lines at position len(new_lines) - (j - i) + output_filename_line
            insert_pos = len(new_lines) - (j - i) + output_filename_line
            new_lines.insert(insert_pos + 1, ' ' * indent + 'max_files=15,')
        
        # Add the remaining lines of this ToolSpec (already added in the loop above)
        # Actually, we already added them in the while loop
        i = j
        continue
    
    i += 1

content = '\n'.join(new_lines)

# Write back
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done!")