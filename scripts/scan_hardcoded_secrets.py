"""扫描工作区源码中硬编码的凭据。

只输出变量名、行号、值长度和前 3 位，绝不输出完整值。

三类检测（2026-09-22 扩充）
--------------------------
1. **凭据字面量**（PAT / PAT2 / PAT3）—— 变量名含 token/key/secret/password 等，
   值是一个足够长的随机串。PAT3 是本次新增的：PAT/PAT2 的值字符集是
   ``[A-Za-z0-9_\\-\\.]``，**不含符号**，所以 `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'`
   这种带 ``!`` 的密码整条漏掉（实测于对标项目 `cxqc168-wq/gpt-register-pro`
   的 `batch-oauth.js`，那里明文密码就带 ``!``）。
   🔴 上面这个示例值已于 2026-09-23 **脱敏**：原值是那个项目里的**真实密码**。
   记录别人的泄漏时不要把泄漏值一起搬过来 —— 引用时只写「文件:行 + 类型」。
   而 PAT/PAT2 原本还有个
   ``val.startswith('http')`` 的短路，把 URL 形态整类跳过。
2. **公网 IP 端点**（PAT_IP）—— 硬编码的 ``http(s)://<公网IP>:<端口>`` 是
   **基础设施标识**，不是凭据，但同样不该随源码发布（对标项目把授权服务器
   明文 IP ``http://64.90.20.244:8443`` 写死在 `licenseGuard.js` 里）。
   私网/回环/链路本地/文档用段一律跳过，否则日志与本地配置会淹没结果。
3. **示例文件与源码的取值矛盾**（跨文件检查）—— 同一个键在 ``*.example.*``
   里是空值或占位符，在源码里却是高熵真值。这是**最强的结构性信号**：
   真实凭据与「示例」的定义互斥，而它不依赖任何密钥格式假设。
   对标项目正是这样漏的：`.gitignore` 挡得住 ``config.json``，挡不住写在
   `visual-qa-capture.js` 里的真 Key，而路径闸门说「这个文件该提交」、
   内容闸门说「不是我的后缀」，两边都放行。

"宁可漏也不吵" 仍然成立：误报会被 ``--no-verify`` 绕过，门禁等于没有。
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

#: 变量名里的「凭据词」。**按段匹配，不做子串匹配。**
#:
#: 🔴 2026-09-22 修正：原实现是
#: ``(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:token|key|...|password|...)[A-Za-z0-9_]*)``
#: —— 前缀**必填**，于是名字恰好等于关键字时前缀没有字符可让，`password = "..."`
#: 一个都匹配不上。实测（PAT/PAT2/PAT3 三模式）：
#:
#:     password  False False False      PASSWORD  False True  False
#:     secret    False False False      SECRET    False True  False
#:     token     False False False      TOKEN     False True  False
#:     pwd       False False False      key       False False False
#:
#: 即**小写形态三个模式全漏**，只有全大写靠 PAT2 兜住 —— 而 `password = "..."`
#: 恰恰是最可能的硬编码写法。改成按段匹配后，`password` / `secret` / `token` /
#: `pwd` / `key` 都被抓住。
#:
#: 用段而不是子串，是为了顺手消掉一整类误报：`keyboard` / `author` / `tokenizer` /
#: `secretary` 的开头恰好是 key/auth/token/secret，子串匹配会把它们全判成凭据。
#: 这类误报的处置方式是 `--no-verify`，门禁等于没有。
_CREDENTIAL_WORDS = frozenset((
    'token', 'tokens', 'key', 'keys', 'secret', 'secrets',
    'password', 'passwords', 'passwd', 'pwd', 'auth', 'credential', 'credentials',
    # 连写形态：按 `_`/`-` 分段看不见它们（`apikey` 是一整段），但语义就是凭据。
    'apikey', 'apisecret', 'accesstoken', 'refreshtoken', 'idtoken',
    'clientsecret', 'secretkey', 'privatekey', 'signingkey',
))
_NAME_SEPARATOR = re.compile(r'[^A-Za-z0-9]+')
#: camelCase / PascalCase 的切分器，覆盖 `accessToken` / `RefreshToken` 这类没有分隔符的写法
_CAMEL_WORD = re.compile(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+')


def credential_name(name):
    """变量名里有没有一个「段」就是凭据词。

    ``password`` / ``api_token`` / ``SESSION_TOKEN_ENV`` / ``accessToken`` / ``apikey``
    全部为 True；``keyboard`` / ``author`` / ``tokenizer`` / ``passwordless`` 为 False。
    """
    text = str(name or '')
    if any(segment in _CREDENTIAL_WORDS for segment in _NAME_SEPARATOR.split(text.lower())):
        return True
    return any(word.lower() in _CREDENTIAL_WORDS for word in _CAMEL_WORD.findall(text))


#: 一条赋值/键值对。名字与值都从同一处取出，模式只剩「值长什么样」一个变量 ——
#: 这样名字判定（``credential_name``）就与值判定正交，不会出现「换个值字符集
#: 就要再抄一遍名字正则」的情况（PAT/PAT2/PAT3 三份重名正则正是这么分叉的）。
_ASSIGN = re.compile(
    r'''["']?(?P<name>[A-Za-z_][A-Za-z0-9_]*)["']?\s*[:=]\s*(?P<quote>["'])(?P<val>[^"'\r\n]+)(?P=quote)'''
)

#: 普通值：无符号的足够长随机串。16 位起，`abcdEFGH1234ijkl` 这类刚好压线。
_PLAIN_VALUE = re.compile(r'^[A-Za-z0-9_\-\.]{16,}$')
#: 符号值：可以带 !@#$%^&*()-+= 等。门槛低到 12 是因为符号本身就是很强的信号 ——
#: 实测 `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'` 这种带 `!` 的密码会被纯字符集版整条漏掉。
_SYMBOL_VALUE = re.compile(
    r'^[A-Za-z0-9_\-\.\!\@\#\$\%\^\&\*\(\)\+\=\~\`\|\[\]\{\}\:\;\,<>\?]{12,}$'
)

# 硬编码公网 IP 端点。私网/回环/链路本地/文档段不算 —— 那些在本地配置与日志里
# 是正常的，报出来只会让门禁变吵。
PAT_IP = re.compile(r'\bhttps?://(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?::(?P<port>\d{2,5}))?')

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
#   passwordless —— `PASSWORDLESS_SIGNUP_CODE = "identity_provider_mismatch"`
#                   是「无密码注册撞上身份提供方」的错误码。改成按段匹配后它
#                   已经天然不会被判成凭据（`passwordless` 是一整段，不等于
#                   `password`），这一项留着只是为了意图清楚。
# 原则：宁可漏也不吵。一吵就被 --no-verify 绕过，门禁等于没有。
VARNAME_SKIP = re.compile(
    r'(site[_-]?key|probe|placeholder|persistence|fallback|unauthorized|error|status|passwordless)',
    re.I,
)

# 名字以 `_ENV` / `_VAR` 结尾的，存的是**环境变量名**，不是值本身。
# 实测：`SESSION_TOKEN_ENV = "PP_SESSION_TOKEN"`（services/protocol-payment/common/
# file_loading.py:57）是各提取器读取 session cookie 时查的那个环境变量名，
# 字面量就是查找键，里面没有秘密 —— 但它含 TOKEN 且长度 16，被 PAT2 命中。
# 用**后缀**而不是子串来跳过，`ENVIRONMENT_TOKEN = "ghp_..."` 这类仍会被抓。
VARNAME_SKIP_SUFFIX = re.compile(r'(_env|_env_name|_var|_var_name)$', re.I)


# tests/ 不扫：测试必须构造假凭据才能验证脱敏逻辑，通用高熵匹配在这里必然误报
# （本仓实测 11 条命中全是 fixture 假值）。测试文件由 test_precommit_guard.py
# 扫描全量跟踪文件来兜底，那里才是有效的防线。
SKIP_TESTS = True

# 示例/模板文件的名字。真实凭据与「示例」的定义互斥，所以只要同名键在示例里是
# 空值或占位符、在源码里是高熵值，就一定是有人把真值粘进了源码。
EXAMPLE_FILE_RE = re.compile(r'\.(example|sample|template)\.', re.I)

#: 公网 IP 的排除段：回环 / 私网 / 链路本地 / 文档与测试用段 / 组播以上。
_PRIVATE_IP_PREFIXES = ('10.', '127.', '192.168.', '169.254.', '0.')
_DOC_IP_PREFIXES = ('192.0.2.', '198.51.100.', '203.0.113.')
_IP_RE = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')


def is_public_ip(value):
    """True only for routable-looking addresses worth flagging."""
    match = _IP_RE.match(str(value or '').strip())
    if not match:
        return False
    octets = [int(part) for part in match.groups()]
    if any(octet > 255 for octet in octets):
        return False
    text = '.'.join(str(octet) for octet in octets) + '.'
    if text.startswith(_PRIVATE_IP_PREFIXES) or text.startswith(_DOC_IP_PREFIXES):
        return False
    if 224 <= octets[0]:
        return False
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return False
    return True


def looks_like_secret_value(value):
    """Keep prose, paths and identifiers out of the value match.

    要求「字母和数字都有」是这里最强的一道闸：``not-a-real-secret``、
    ``authenticated``、``auth_state_failed`` 这类全小写散文/标识符值一个都过不去，
    而真实凭据几乎必然同时含字母与数字。

    🔴 两个值分支（``_PLAIN_VALUE`` / ``_SYMBOL_VALUE``）**都要过这一道**。
    只给符号分支加会让 ``auth_state = "authenticated"`` 这种普通映射漏进来 ——
    实测就是这样在 ``accounts/account_scan.py:53`` 报了一条假阳性。
    """
    text = str(value or '')
    if text.lower().startswith(('http://', 'https://')):
        return False
    if text.startswith(('/', '\\', './', '../')):
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    if not any(ch.isdigit() for ch in text):
        return False
    return True


def iter_scannable_lines(text, suffix):
    """Yield ``(lineno, line)``, skipping comments and docstrings.

    docstring 示例是本类扫描最大的假阳性来源 —— ``accounts/account_2fa.py`` 的
    ``{"totp_secret": "JBSWY3DPEHPK3PXP"}`` 是 RFC 4226 教科书里的示例密钥，
    长度和字符分布与真密钥毫无区别，**只有上下文能区分**，所以只能按上下文跳过。

    🔴 ``scripts/precommit_guard.py`` 里有一份等价实现（同一个坑，两个闸门各踩一次）。
    ``scripts/`` 不是包、也没有 ``common``，仓内脚本互不导入（只把仓库根加进
    ``sys.path`` 后导入 ``sms_tool``），所以这里刻意重复而不是跨脚本 import。
    代价是两份可能漂移 —— 由 ``tests/test_scan_hardcoded_secrets.py`` 的平价测试
    钉住：改动任一份而不同步另一份，CI 会红。
    """
    in_docstring = False
    for number, line in enumerate(str(text).splitlines(), 1):
        stripped = line.strip()
        if suffix in ('.py', '.sh', '.ps1', '.yml', '.yaml'):
            if stripped.startswith('#'):
                continue
            if '"""' in line or "'''" in line:
                # 三引号出现奇数次才切换 docstring 状态
                if (line.count('"""') + line.count("'''")) % 2 == 1:
                    in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
        elif suffix == '.cs':
            if stripped.startswith(('//', '/*', '*', '///')):
                continue
        yield number, line


#: OAuth 里**设计上就公开**的标识符。PKCE 公开客户端没有 secret，`client_id`
#: 必须随源码分发（本仓 `codex_oauth.py:35` 是 Codex CLI 的公开 client id，
#: `codex_export.py:333` 还拿它当 fallback）。跨文件检查看到「示例留空、源码填值」
#: 会把它判成泄漏，而它恰恰是唯一正确的写法。
#: 用**后缀**匹配，`CLIENT_ID_SECRET` / `CLIENT_ID_FILE` 这类仍会被抓。
PUBLIC_IDENTIFIER_SUFFIX = re.compile(
    r'(^|_)(client|app|application|tenant|project|subscription)_id$', re.I
)


def is_public_identifier(name):
    """True for keys that are identifiers by spec, not secrets.

    ``-`` 与 ``_`` 都是合法的键分隔符（JSON 配置用下划线，HTTP 头与 C# 属性用连字符），
    所以先归一化再匹配 —— 否则 ``OAuth-Client-Id`` 会漏过去。
    """
    return bool(PUBLIC_IDENTIFIER_SUFFIX.search(
        str(name or '').strip().lower().replace('-', '_')))


def is_documentation_sample(line, match):
    """True when the match sits inside inline-code backticks.

    泄漏**形态**写在散文里（`` ``password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'`` ``）是文档，
    不是泄漏。没有这条，本脚本记录自己新增的检测形态时就会自我命中 —— 而下一个
    维护者会用「豁免这个文件」来"修复"，那反而更糟：真凭据粘进该文件将无人看见。
    要求反引号**紧邻**两侧，所以围栏代码块里的裸值照样会被抓。
    """
    start, end = match.start(), match.end()
    return (start > 0 and end < len(line)
            and line[start - 1] == '`' and line[end] == '`')


def example_placeholder_keys(root=ROOT):
    """Keys that an ``*.example.*`` file declares as empty or a placeholder.

    Returns ``{normalized_key: (rel_path, raw_value)}``.  This is the reference
    set for the cross-file check: a key defined here as ``""`` must not appear in
    source carrying a real-looking value.
    """
    keys = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not EXAMPLE_FILE_RE.search(filename):
                continue
            if os.path.splitext(filename)[1].lower() not in EXTS:
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
                    text = handle.read()
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            for match in re.finditer(r'''["']?(?P<key>[A-Za-z_][A-Za-z0-9_]*)["']?\s*[:=]\s*["'](?P<val>[^"']*)["']''', text):
                value = match.group('val').strip()
                if value and not PLACEHOLDER.match(value):
                    continue
                keys.setdefault(match.group('key').lower(), (rel, value))
    return keys


def scan_line(line, example_keys=None):
    """Return findings for one line: ``[(kind, name, value, line_reason)]``."""
    found = []
    for match in _ASSIGN.finditer(line):
        name = match.group('name')
        value = match.group('val')
        if not credential_name(name):
            continue
        if VARNAME_SKIP.search(name) or VARNAME_SKIP_SUFFIX.search(name):
            continue
        if is_public_identifier(name) or is_documentation_sample(line, match):
            continue
        if PLACEHOLDER.match(value) or value.lower().startswith('http'):
            continue
        if not looks_like_secret_value(value):
            continue
        if _PLAIN_VALUE.match(value):
            found.append(('literal', name, value, ''))
        elif _SYMBOL_VALUE.match(value):
            found.append(('symbol-literal', name, value, ''))

    match = PAT_IP.search(line)
    if match and is_public_ip(match.group('ip')) and not is_documentation_sample(line, match):
        found.append(('public-endpoint', match.group('ip'), match.group('ip'), match.group('port') or ''))

    if example_keys:
        for match in re.finditer(r'''["']?(?P<key>[A-Za-z_][A-Za-z0-9_]*)["']?\s*[:=]\s*["'](?P<val>[^"']+)["']''', line):
            key = match.group('key').lower()
            value = match.group('val')
            if key not in example_keys:
                continue
            if is_public_identifier(key) or is_documentation_sample(line, match):
                continue
            if PLACEHOLDER.match(value):
                continue
            if not looks_like_secret_value(value):
                continue
            example_path, _ = example_keys[key]
            found.append(('inline-vs-example', match.group('key'), value,
                          'example file has it blank (%s)' % example_path))
    return found


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


def main():
    example_keys = example_placeholder_keys()
    findings = []
    scanned = 0

    for fp in iter_files():
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except OSError:
            continue
        scanned += 1
        if SKIP_TESTS and os.sep + 'tests' + os.sep in fp:
            continue
        rel = os.path.relpath(fp, ROOT)
        suffix = os.path.splitext(fp)[1].lower()
        for i, line in iter_scannable_lines(text, suffix):
            for kind, name, value, reason in scan_line(line, example_keys):
                findings.append((rel, i, kind, name, len(value), value[:3], reason))

    print('scanned files:', scanned)
    print('example keys tracked:', len(example_keys))
    print()
    print('%-44s %6s %-16s %-24s %5s %s' % ('FILE', 'LINE', 'KIND', 'VAR', 'LEN', 'PREFIX'))
    print('-' * 115)
    for rel, ln, kind, name, vlen, pre, reason in sorted(set(findings)):
        print('%-44s %6d %-16s %-24s %5d %s... %s' % (rel[:44], ln, kind, name[:24], vlen, pre, reason))
    print()
    print('total findings:', len(set(findings)))

    # 没有这一步，本脚本永远 exit 0，CI 上就是个装饰品。
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
