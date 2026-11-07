import re

# Debug script to understand the structure
with open(r"C:\Users\Rudi Hadiman\Downloads\convert\backend\app\services\registry.py", "r", encoding="utf-8") as f:
    content = f.read()

# Check for ToolSpec pattern
matches = re.findall(r'ToolSpec\([^)]+id="([^"]+)"', content)
print(f"Found {len(matches)} ToolSpecs: {matches[:10]}...")

# Check line by line
lines = content.split('\n')
for i, line in enumerate(lines):
    if 'ToolSpec(' in line and 'id="' in line:
        print(f"Line {i+1}: {line.strip()[:80]}")