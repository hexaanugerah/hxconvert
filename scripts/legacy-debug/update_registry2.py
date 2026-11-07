import re

# Read the registry file
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# First, update merge-pdf from max_files=10 to max_files=15
content = content.replace(
    'max_files=10,',
    'max_files=15,'
)

# Split content by ToolSpec to process each one
# We'll use a more robust approach: find each ToolSpec block and add max_files if missing

# Pattern to find each ToolSpec
tool_spec_pattern = r'(ToolSpec\([^)]+?\))'

# Actually, let's do it line by line more carefully
# Find all ToolSpec starts and ends
lines = content.split('\n')
new_lines = []
i = 0
in_tool_spec = False
tool_spec_start = -1
brace_count = 0
current_tool_id = None

while i < len(lines):
    line = lines[i]
    
    # Check for ToolSpec start
    if 'ToolSpec(' in line and 'id="' in line:
        in_tool_spec = True
        tool_spec_start = i
        # Extract tool id
        match = re.search(r'id="([^"]+)"', line)
        if match:
            current_tool_id = match.group(1)
        brace_count = line.count('(') - line.count(')')
    
    if in_tool_spec:
        brace_count += line.count('(') - line.count(')')
        
        # Check if this ToolSpec already has max_files
        if 'max_files=' in line:
            pass  # already has it
        
        # Check for end of ToolSpec (closing brace at same level)
        if brace_count <= 0 and in_tool_spec:
            # ToolSpec ends here
            # Check if max_files was in this block
            tool_block = '\n'.join(lines[tool_spec_start:i+1])
            if 'max_files=' not in tool_block and current_tool_id not in ['merge-pdf', 'image-to-pdf', 'merge-docx', 'pdf-to-word', 'pdf-to-images']:
                # Need to add max_files=15 after output_filename
                # Find the output_filename line in this block
                for j in range(tool_spec_start, i+1):
                    if 'output_filename=' in lines[j]:
                        # Insert max_files=15 after this line
                        indent = len(lines[j]) - len(lines[j].lstrip())
                        new_lines.append(lines[j])
                        new_lines.append(' ' * indent + 'max_files=15,')
                        # Add remaining lines
                        for k in range(j+1, i+1):
                            new_lines.append(lines[k])
                        break
                else:
                    # Didn't find output_filename, just add all lines
                    new_lines.extend(lines[tool_spec_start:i+1])
            else:
                new_lines.extend(lines[tool_spec_start:i+1])
            
            in_tool_spec = False
            current_tool_id = None
            i += 1
            continue
    
    if not in_tool_spec:
        new_lines.append(line)
    
    i += 1

# Handle any remaining lines
if in_tool_spec:
    new_lines.extend(lines[tool_spec_start:])

content = '\n'.join(new_lines)

# Write back
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done!")