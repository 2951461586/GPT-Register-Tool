import subprocess
import hashlib
import sys

FILES = ['.gitignore', 'README.md', 'sms_tool/cli.py', 'sms_tool/config.py', 'pytest.ini']

print('file | index_md5 | wt_md5(crlf->lf) | identical')
print('-' * 78)

for f in FILES:
    r = subprocess.run(['git', 'cat-file', 'blob', ':' + f], capture_output=True)
    blob = r.stdout
    try:
        wt = open(f, 'rb').read()
    except OSError as e:
        print(f'{f}: read error {e}')
        continue
    wt_lf = wt.replace(b'\r\n', b'\n')
    same = (blob == wt_lf)
    print('%-28s %s %s %s  idx=%d wt=%d wtlf=%d' % (
        f,
        hashlib.md5(blob).hexdigest()[:12],
        hashlib.md5(wt_lf).hexdigest()[:12],
        'SAME' if same else 'DIFF',
        len(blob), len(wt), len(wt_lf),
    ))
