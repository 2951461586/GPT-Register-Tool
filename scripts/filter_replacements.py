"""从 extract_history_secrets.py 产出的候选里，筛出真正需要替换的密钥。

保留规则（避免误伤测试 fixture 与普通英文单词）：
  1. 32 位纯 hex
  2. nm_ 开头且长度 >= 20
  3. 长度 8-12 的纯字母数字、且至少含一个数字（排除 password/secret 这类单词）
"""

import os
import re

SRC = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.txt'
DST = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.filtered.txt'

HEX32 = re.compile(r'^[0-9a-f]{32}$')
NM = re.compile(r'^nm_[A-Za-z0-9_\-]{20,}$')
SHORT_PW = re.compile(r'^(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{8,12}$')

kept, dropped = [], []

with open(SRC, 'rb') as f:
    for line in f:
        raw = line.split(b'==>')[0]
        try:
            val = raw.decode('ascii')
        except UnicodeDecodeError:
            dropped.append((raw[:8], 'non-ascii'))
            continue
        if HEX32.match(val):
            kept.append(raw)
        elif NM.match(val):
            kept.append(raw)
        elif SHORT_PW.match(val):
            kept.append(raw)
        else:
            dropped.append((raw[:10], f'len={len(val)}'))

with open(DST, 'wb') as f:
    for v in kept:
        f.write(v + b'==>***REMOVED***\n')

print(f'保留 {len(kept)} 条需要替换的真密钥:')
for v in kept:
    print(f'  len={len(v):3d}  {v[:6].decode()}...')
print(f'\n丢弃 {len(dropped)} 条（测试假值/普通单词）:')
for pre, why in dropped[:12]:
    print(f'  {pre.decode("ascii", "ignore")}...  ({why})')
print(f'\n已写入: {DST}')
