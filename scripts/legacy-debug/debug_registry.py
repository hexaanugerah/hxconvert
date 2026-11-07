# Debug script to understand the structure
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find all ToolSpec starts
for i, line in enumerate(lines):
    if 'ToolSpec(' in line and 'id="' in line:
        match = re.search(r'id="([^"]+)"', line)
        if match:
            tool_id = match.group(1)
            # Count braces from here
            brace_count = line.count('(') - line.count(')')
            j = i + 1
            while j < len(lines) and brace_count > 0:
                l = lines[j]
                brace_count += l.count('(') - l.count(')')
                if 'max_files=' in l:
                    print(f"{tool_id}: HAS max_files at line {j+1}")
                    break
                j += 1
            else:
                print(f"{tool_id}: NO max_files, brace_count ended at {brace_count}, lines {i+1}-{j}")

import re