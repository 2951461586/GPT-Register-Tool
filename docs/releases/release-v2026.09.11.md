# v2026.09.11

本版本将注册代理出口整体切换到越南（VN），补齐 9http 供应商模板与两侧地理档案，
并收敛 PayPal 链接生成中的代理地区重写实现。

## 注册代理与出口地区

- 注册主池（`proxy.json` 的 `proxy.registration` / `default` / `pool`）由 IPWO US
  切换为 20 条越南出口：9http（`global.9http.com:9091`，`geo-VN` 模板）10 条 +
  IPWO（`us.ipwo.net:7878`，`custom_zone_VN` 模板）10 条，出口实测落在 FPT Telecom /
  VNPT / Viettel 等本地 ISP。
- 协议注册与无头浏览器注册两条路径共用该池，活体/健康探测也按其回退，出口随之切换。
- 新增 9http `geo-XX` 供应商模板支持：地区识别、地区重定、会话轮换三项齐备；重定地区时
  **保留原标签**，`geo-VN` 不会被改写成供应商不认的 `region-US`。
- 补齐 VN 地理档案，使指纹与出口严格一致：协议路径 `_GEO_PROFILES["VN"]` =
  `Asia/Ho_Chi_Minh` / `vi-VN`；浏览器路径补 `BROWSER_LOCALE_PROFILES["vn"]`、
  `COUNTRY_LOCALE_PROFILE_MAP["VN"]` 与 `TIMEZONE_NAME_BY_IANA`。缺表会静默回退成
  "别国出口 + 美式语言"，故一并收口。

## 支付链路

- `pp_link_helpers.proxy_for_country_template` 不再自建 `region-XX` 重写，改为委托
  `phone_proxy.match_proxy_region`（→ `proxy_entry.retarget_region`）。此前它只认
  `region-XX`，9http / IPWO / Kookeey 模板会被原样返回，调用方误以为已切换地区。
- 仅保留 canonical 不负责的两处形态：Cliproxy `-st-<state>-city-<city>` 的 JP 例外，
  以及无密码 `user-XX` 的尾部回退。
- 新增供应商模板此后只需改 `proxy_entry` 一处，注册与支付两条 lane 同时生效。

## 发布链路加固（安全）

本次在收尾核对发布自洽性时，查出并修掉两个**独立**缺陷，二者都会让带凭据的
文件进入发布资产：

- **凭据配置快照未被忽略**：`proxy.json.bak-vn-migration`（100 条代理账密 + 一个
  smsbower api_key）以未跟踪状态躺在公开仓库根目录。`.gitignore` 只有精确的
  `proxy.json`，既有的 `*.bak` / `*.bak_before_*` / `*.bak_*` 三条规则都挡不住
  **连字符**后缀 `.bak-vn-migration`；`precommit_guard.BLOCKED_NAMES` 同样是精确名
  匹配。现两层都按**家族**判定（`config|proxy|runtime|payment|session.json` 及其任意
  快照后缀），并保留 `proxy.json.example` / `config.json.example` 这类示例模板例外。
- **发布载荷闸门是 fail-open 的**：`scripts/scan_release_payload.py` 只把
  "source-ish 后缀白名单"里的文件送去问 git。`proxy.json.bak-vn-migration` 的 suffix
  是 `.bak-vn-migration`，不在白名单里 —— 于是**即使 `.gitignore` 已经拒绝它，闸门
  也照样放行**。闸门逻辑本身没坏，是它的前提假设不成立。现改为 fail-closed：只跳过
  真正的构建产物（`dist/`、`scripts/installer/{bin,obj}` 与 `.dll/.exe/.pdb` 等），
  其余一律送去问 git。改动后对上一版真实载荷多查 16 个文件，**零误报**。

附带修掉一个**载荷自洽性**缺陷：载荷取自 `git ls-files`，它只列已跟踪路径，
于是"刚写好、尚未提交的 release note"会被静默丢掉 —— 包里 README 指向一个包里
根本没有的 release note。现改为 `--cached --others --exclude-standard`，
未跟踪但未被忽略的文件一并入包（带凭据的快照因上面第一条而仍然排除在外）。

三层防护均配回归测试与变异验证：`tests/test_config_family_ignore_rules.py`
断言 `.gitignore` 与 pre-commit 守卫的判定必须**逐项一致**（两层漂移成
"一个放行一个拦截"时不会有任何报错，只有泄漏），并对闸门的 fail-closed 行为
做正反双向断言。四个变异体（摘掉忽略规则、摘掉守卫正则、把闸门改回 fail-open、
把备份塞进载荷目录）全部被杀死。

## 测试与验证

- 全量 Python 测试：`3377 passed, 6 skipped, 608 subtests`，0 failed。
- 新增 9http `geo-XX` 模板测试、locale 映射完整性守卫、三种供应商模板的继承测试，
  以及发布链路守卫 `tests/test_config_family_ignore_rules.py`（含闸门 fail-closed 正反断言）。
- 对新增守卫执行变异验证（共 10 个变异体，全部被杀死 / 被拦截）。
- 代理实测：20 条中 18 条可用，出口全部确认落地越南。
- WPF Release/win-x64 发布成功，规范产物为 `dist/net10/SmsWorkbench.exe`。

## 发布资产

- Windows 安装器：`GPT-Register-Tool-Setup-v2026.09.11.exe`
- Windows 便携包：`GPT-Register-Tool-win-x64-v2026.09.11.zip`
- SHA-256 校验清单：`GPT-Register-Tool-v2026.09.11.sha256.txt`

本版本未将 `config.json`、`runtime/`、`sessions/`、邮箱凭据、代理凭据或 Token 纳入发布提交和资产。
