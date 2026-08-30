"""把 _diag_roxy_egress.py 里的硬编码凭据改为环境变量读取。

用脚本替换而不是手工 Edit，避免明文密钥再次出现在对话/工具调用记录里。
"""

import re

TARGET = r'F:\epsoft\GPT-Register-Tool\scripts\_diag_roxy_egress.py'

with open(TARGET, encoding='utf-8') as f:
    src = f.read()

orig_token_line = [l for l in src.splitlines() if l.startswith('TOKEN =')]
orig_proxy_line = [l for l in src.splitlines() if l.startswith('PROXY_URL =')]

# 1) TOKEN -> 环境变量
src, n1 = re.subn(
    r'^TOKEN = "[^"]*"',
    'TOKEN = os.environ.get("ROXY_API_TOKEN", "")',
    src, flags=re.M)

# 2) PROXY_URL -> 环境变量
src, n2 = re.subn(
    r'^PROXY_URL = "http[^"]*"',
    'PROXY_URL = os.environ.get("DIAG_PROXY_URL", "")',
    src, flags=re.M)

# 3) 补 import os
if re.search(r'^import os$', src, flags=re.M) is None:
    src = src.replace('import json\n', 'import json\nimport os\n', 1)

# 4) 注释说明
src = src.replace(
    'TOKEN = os.environ.get("ROXY_API_TOKEN", "")',
    '# 敏感信息走环境变量，禁止硬编码（曾误提交进版本库）\n'
    'TOKEN = os.environ.get("ROXY_API_TOKEN", "")',
    1)

# 5) main() 开头加校验
old_main = 'def main():\n    from playwright.sync_api import sync_playwright'
new_main = (
    'def main():\n'
    '    if not TOKEN or not PROXY_URL:\n'
    '        print("[!] 请先设置环境变量 ROXY_API_TOKEN 与 DIAG_PROXY_URL")\n'
    '        return 1\n\n'
    '    from playwright.sync_api import sync_playwright'
)
src = src.replace(old_main, new_main, 1)

# 6) docstring 补用法
src = src.replace(
    '用法: python scripts/_diag_roxy_egress.py',
    '用法:\n'
    '    set ROXY_API_TOKEN=xxx\n'
    '    set DIAG_PROXY_URL=http://user:pass@host:port\n'
    '    python scripts/_diag_roxy_egress.py',
    1)

with open(TARGET, 'w', encoding='utf-8', newline='') as f:
    f.write(src)

print('TOKEN 行替换:', n1)
print('PROXY_URL 行替换:', n2)
print()
print('=== 替换后关键行（不含任何明文值） ===')
for i, line in enumerate(src.splitlines(), 1):
    s = line.strip()
    if s.startswith(('TOKEN =', 'PROXY_URL =', 'WS =', 'PROJ =', 'API =',
                     'import os', 'if not TOKEN')):
        print(f'{i:4d}: {s}')
