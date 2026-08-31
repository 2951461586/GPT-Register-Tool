"""扫描 git 历史中指定路径的内容，脱敏输出疑似凭据字段。

用法: python scan_sensitive_history.py <path> [more paths...]
"""

import re
import subprocess
import sys

# Output must stay ASCII. On a GitHub-hosted Windows runner stdout is cp1252 and
# any non-Latin1 character raises UnicodeEncodeError, killing the step (which
# then skips every later step in the job). Same for the other scan scripts.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, OSError, ValueError):
    pass

PAT = re.compile(r'(key|token|secret|password|passwd|pwd|auth|cookie|session)', re.I)


def mask(v, keep=3):
    if not isinstance(v, str):
        return v
    v = v.strip()
    if len(v) <= keep:
        return '*' * len(v)
    return v[:keep] + '*' * min(len(v) - keep, 12) + f'(len={len(v)})'


def mask_text(text):
    """对 key=value / "key": "value" 形式做脱敏"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r'^"?([A-Za-z0-9_\-\.]+)"?\s*[:=]\s*"?([^",]*)"?', stripped)
        if m and PAT.search(m.group(1)):
            k, v = m.group(1), m.group(2)
            if v and not v.startswith(('http', '{', '[', 'true', 'false', 'null')):
                line = line.replace(v, mask(v))
        out.append(line)
    return '\n'.join(out)


def main():
    paths = sys.argv[1:]
    if not paths:
        print('need at least one path argument')
        return 1

    for p in paths:
        print('=' * 70)
        print('PATH:', p)
        r = subprocess.run(
            ['git', 'log', '--all', '--format=%H', '--', p],
            capture_output=True, text=True,
        )
        commits = [c for c in r.stdout.split() if c]
        if not commits:
            print('  (not present in history)')
            continue
        print(f'  present in {len(commits)} commit(s)')
        # 只看最新版本
        head = commits[0]
        r2 = subprocess.run(
            ['git', 'show', f'{head}:{p}'], capture_output=True, text=True,
        )
        if r2.returncode != 0:
            print('  (read failed)')
            continue
        content = r2.stdout
        print('  --- latest version (masked) ---')
        masked = mask_text(content)
        for line in masked.splitlines():
            if PAT.search(line):
                print('   ', line.strip()[:120])
        print('  --- lines matching sensitive keywords:',
              sum(1 for l in content.splitlines() if PAT.search(l)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
