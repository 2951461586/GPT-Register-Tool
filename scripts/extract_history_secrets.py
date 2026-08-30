"""从全部 git 历史对象中提取疑似真实密钥，生成 filter-repo 的 replace-text 映射。

明文只写入 runtime/ 下的 replacements.txt（该目录已被 .gitignore 忽略），
stdout 只输出脱敏统计，避免密钥进入对话记录。
"""

import collections
import re
import subprocess
import os

OUT = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.txt'

# 高置信度密钥形态
PATS = [
    re.compile(rb'\b[0-9a-f]{32}\b'),                 # 32 位 hex（roxy token 等）
    re.compile(rb'\bnm_[A-Za-z0-9_\-]{20,}\b'),       # smailr
    re.compile(rb'\brk-[A-Za-z0-9_\-]{20,}\b'),       # remail
    re.compile(rb'\bcfat_[A-Za-z0-9_\-]{20,}\b'),     # cfworker
    re.compile(rb'\bsk-[A-Za-z0-9_\-]{20,}\b'),       # sub2api / openai 风格
    re.compile(rb'://[A-Za-z0-9_\-\.]+:([A-Za-z0-9_\-\.!$%^&*]{6,})@'),  # 代理 URL 里的口令
]

# 明显是测试/示例值，跳过
SKIP = re.compile(
    rb'test|dummy|example|sample|placeholder|fixture|fake|changeme|redacted'
    rb'|private|not-|<xxx|your[_-]', re.I)


def main():
    out = subprocess.run(['git', 'rev-list', '--objects', '--all'],
                         capture_output=True, text=True).stdout
    shas = [l.split()[0] for l in out.splitlines() if l.strip()]
    print(f'扫描对象数: {len(shas)}')

    data = subprocess.run(
        ['git', 'cat-file', '--batch'],
        input='\n'.join(shas).encode(), capture_output=True,
    ).stdout

    found = collections.Counter()
    pos = 0
    total = len(data)
    while pos < total:
        nl = data.find(b'\n', pos)
        if nl < 0:
            break
        header = data[pos:nl].decode('ascii', 'ignore').split()
        pos = nl + 1
        if len(header) < 3:
            continue
        typ, size = header[1], int(header[2])
        content = data[pos:pos + size]
        pos += size + 1
        if typ != 'blob' or size > 2_000_000:
            continue
        for p in PATS:
            for m in p.finditer(content):
                val = m.group(1) if p.groups else m.group(0)
                if SKIP.search(val):
                    continue
                found[val] += 1

    print(f'\n候选密钥数: {len(found)}')
    print('%-14s %6s %8s' % ('PREFIX', 'LEN', '出现次数'))
    print('-' * 34)
    for val, cnt in sorted(found.items(), key=lambda kv: -kv[1]):
        print('%-14s %6d %8d' % (val[:12].decode('ascii', 'ignore'), len(val), cnt))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'wb') as f:
        for val in found:
            f.write(val + b'==>***REMOVED***\n')
    print(f'\n已写入: {OUT}  ({len(found)} 条)')


if __name__ == '__main__':
    main()
