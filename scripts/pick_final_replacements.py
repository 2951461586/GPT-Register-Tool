"""从筛选结果中精确挑出已确认为真实凭据的 3 条。

其余 32 位 hex 经核实是代码里的哈希常量（含空串 MD5 d41d8cd9...），
替换会破坏代码语义，故排除。
"""

SRC = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.filtered.txt'
DST = r'F:\epsoft\GPT-Register-Tool\runtime\_filter_repo_work\replacements.final.txt'

# 已确认的真实凭据前缀：roxy token / 旧 smailr key / 代理账号口令
WANT = (b'556c27', b'nm_010', b'451203')

out = []
with open(SRC, 'rb') as f:
    for line in f:
        val = line.split(b'==>')[0]
        if val.startswith(WANT):
            out.append(line)

with open(DST, 'wb') as f:
    f.writelines(out)

print(f'最终替换 {len(out)} 条:')
for line in out:
    v = line.split(b'==>')[0]
    print(f'  len={len(v):3d}  prefix={v[:6].decode()}...')
print(f'\n写入: {DST}')
