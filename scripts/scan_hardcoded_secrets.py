"""扫描工作区源码中硬编码的凭据。

只输出变量名、行号、值长度和前 3 位，绝不输出完整值。
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCAN_DIRS = ['sms_tool', 'scripts', 'services', 'tests', 'SmsWorkbench',
             'SmsWorkbench.Contracts', '.']
SKIP_DIRS = {'.git', '.venv', 'runtime', 'dist', 'sessions', '__pycache__',
             'node_modules', '.pytest_cache', '.workbuddy-ai', 'logs',
             'browser_extensions', 'sentinel', '.agents', '.claude', '.codex'}

# 变量名里含敏感词，且值是足够长的无空格随机串
PAT = re.compile(
    r'''(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:token|key|secret|password|passwd|pwd|auth|credential)[A-Za-z0-9_]*)\s*[:=]\s*["'](?P<val>[A-Za-z0-9_\-\.]{16,})["']''',
    re.I,
)
# 大写常量形式 TOKEN = "xxx" / API_KEY = "xxx"
# 前缀必须可选，否则纯 "TOKEN" 的首字母被 [A-Z] 吃掉后剩下 "OKEN" 匹配不上
PAT2 = re.compile(
    r'''(?P<name>(?:[A-Z][A-Z0-9_]*)?(?:TOKEN|KEY|SECRET|PASSWORD|AUTH)[A-Z0-9_]*)\s*[:=]\s*["'](?P<val>[A-Za-z0-9_\-\.]{16,})["']'''
)

EXTS = {'.py', '.cs', '.json', '.js', '.ts', '.ps1', '.sh', '.md', '.txt', '.yml', '.yaml'}

# 明显的占位/示例值，跳过
PLACEHOLDER = re.compile(
    r'^(your|xxx|placeholder|example|sample|test_|dummy|fake|changeme|redacted|none|todo|abc123|<)',
    re.I,
)

findings = []

for d in SCAN_DIRS:
    base = os.path.join(ROOT, d)
    if not os.path.isdir(base):
        continue
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in EXTS:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                for p in (PAT, PAT2):
                    m = p.search(line)
                    if not m:
                        continue
                    val = m.group('val')
                    if PLACEHOLDER.match(val):
                        continue
                    if val.startswith('http'):
                        continue
                    rel = os.path.relpath(fp, ROOT)
                    findings.append((rel, i, m.group('name'), len(val), val[:3]))

print('%-52s %6s %-26s %5s %s' % ('FILE', 'LINE', 'VAR', 'LEN', 'PREFIX'))
print('-' * 105)
for rel, ln, name, vlen, pre in sorted(set(findings)):
    print('%-52s %6d %-26s %5d %s...' % (rel[:52], ln, name[:26], vlen, pre))
print()
print('total findings:', len(set(findings)))
