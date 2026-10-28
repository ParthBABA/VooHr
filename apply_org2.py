import os

patch_path = r'C:\hr-copilot-mvp\orgonly.patch'
target_path = r'C:\hr-copilot-mvp\test_account_delete_org_confirm.py'

with open(patch_path, 'r', encoding='utf-8', errors='ignore') as f:
    patch = f.read()

lines = patch.splitlines()
start = None
for i, l in enumerate(lines):
    if l.startswith('+++ b/'):
        start = i
        break

if start is None:
    raise SystemExit('no')

content_lines = []
for l in lines[start+1:]:
    if l.startswith('+') and not l.startswith('+++'):
        content_lines.append(l[1:])
    elif l.startswith(' '):
        continue
    elif l.startswith('-'):
        continue
    elif l.startswith('@'):
        continue

content = '\n'.join(content_lines)
if content.startswith('\n'):
    content = content[1:]

with open(target_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('done')
