"""扫描工作区源码中硬编码的凭据。

只输出变量名、行号、值长度和前 3 位，绝不输出完整值。
"""

import os
import re
import sys

# Output must stay ASCII: on a GitHub-hosted Windows runner stdout is cp1252 and
# any non-Latin1 character raises UnicodeEncodeError, which kills the CI step.
# The messages below are already English; this guards future additions.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError, ValueError):
    pass

# scripts/ 的上一级就是仓库根。此前套了 3 层 dirname，得到的是仓库根的**父目录**
# （F:\epsoft），于是 SCAN_DIRS 全部 isdir 失败被静默跳过，只剩 '.' 去扫同级无关目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCAN_DIRS = ['sms_tool', 'scripts', 'services', 'tests', 'SmsWorkbench',
             'SmsWorkbench.Contracts']
SKIP_DIRS = {'.git', '.venv', 'runtime', 'dist', 'sessions', '__pycache__',
             'node_modules', '.pytest_cache', '.workbuddy-ai', 'logs',
             'browser_extensions', 'sentinel', '.agents', '.claude', '.codex',
             'bin', 'obj'}
# 仓库根目录散落的入口脚本（不属于上面任何一个目录）
ROOT_SCRIPTS = ['chatgpt_phone_reg.py', 'start_proxy_pool.py', 'verify_proxy.py']

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
    r'^(your|xxx|placeholder|example|sample|test_|dummy|fake|changeme|redacted|none|todo|abc123|<|__)',
    re.I,
)

# 变量名本身就不是凭据的：
#   site_key  —— reCAPTCHA / hCaptcha 的**公钥**，设计上就随页面公开，不是秘密
#   probe / placeholder / persistence / fallback —— 探测串、占位串、注册表键名
#   unauthorized / error / status —— 错误码常量，只是名字里恰好含 auth/code
# 原则：宁可漏也不吵。一吵就被 --no-verify 绕过，门禁等于没有。
VARNAME_SKIP = re.compile(
    r'(site[_-]?key|probe|placeholder|persistence|fallback|unauthorized|error|status)',
    re.I,
)

# tests/ 不扫：测试必须构造假凭据才能验证脱敏逻辑，通用高熵匹配在这里必然误报
# （本仓实测 11 条命中全是 fixture 假值）。测试文件由 test_precommit_guard.py
# 扫描全量跟踪文件来兜底，那里才是有效的防线。
SKIP_TESTS = True

def iter_files():
    """遍历 SCAN_DIRS 与仓库根散落脚本。

    此前 ROOT 算错导致所有 SCAN_DIRS 都 isdir 失败，这个函数会静默产出空列表。
    因此这里显式校验：配置的扫描目录一个都不存在时直接报硬错误，而不是假装通过。
    """
    seen = set()
    missing = [d for d in SCAN_DIRS if not os.path.isdir(os.path.join(ROOT, d))]
    if len(missing) == len(SCAN_DIRS):
        raise SystemExit(
            'FATAL: none of SCAN_DIRS exists, ROOT is probably wrong. '
            'ROOT=%s\n  missing=%s'
            % (ROOT, missing)
        )
    for d in SCAN_DIRS + ROOT_SCRIPTS:
        base = os.path.join(ROOT, d)
        if os.path.isfile(base):
            candidates = [base]
        elif not os.path.isdir(base):
            print('WARN: scan target does not exist, skipped: %s' % d)
            continue
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in EXTS:
                        candidates.append(os.path.join(dirpath, fn))
        for fp in candidates:
            fp = os.path.normpath(fp)
            if fp in seen:
                continue
            seen.add(fp)
            yield fp


findings = []
scanned = 0

for fp in iter_files():
    try:
        with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except OSError:
        continue
    scanned += 1
    if SKIP_TESTS and os.sep + 'tests' + os.sep in fp:
        continue
    for i, line in enumerate(lines, 1):
        for p in (PAT, PAT2):
            m = p.search(line)
            if not m:
                continue
            if VARNAME_SKIP.search(m.group('name')):
                continue
            val = m.group('val')
            if PLACEHOLDER.match(val):
                continue
            if val.startswith('http'):
                continue
            rel = os.path.relpath(fp, ROOT)
            findings.append((rel, i, m.group('name'), len(val), val[:3]))

print('scanned files:', scanned)
print()
print('%-52s %6s %-26s %5s %s' % ('FILE', 'LINE', 'VAR', 'LEN', 'PREFIX'))
print('-' * 105)
for rel, ln, name, vlen, pre in sorted(set(findings)):
    print('%-52s %6d %-26s %5d %s...' % (rel[:52], ln, name[:26], vlen, pre))
print()
print('total findings:', len(set(findings)))

# 没有这一步，本脚本永远 exit 0，CI 上就是个装饰品。
sys.exit(1 if findings else 0)
