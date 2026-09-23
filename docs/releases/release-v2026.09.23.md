# v2026.09.23

自 v2026.09.14 以来的收口（28 个提交）：接入第二家接码协议族 NexSMS、落地带 TTL 的
环境账本、供应商中立化与静态号池退役、桌面端「一键接码」接通在线目录，以及一批
配置 / 文档 / 安全缺陷修复。

## 新增功能

- **第二接码协议族 NexSMS** —— 传输（REST `/api/...`）、信封（`{code,message,data}`）、
  生命周期键（手机号；该厂商不发 `activationId`）三处都与 sms-activate 不同，故新增
  `nexsms_json` 协议族而非硬塞进原适配器 —— 硬塞会让「就绪 / 完成」变成谎报。
  注册表新增 `IMPLEMENTED_PROTOCOLS`，可选供应商 3 → 4，C# 下拉框同步。
- **桌面端「一键接码」接通 NeXSMS 在线目录** —— 余额 / 国家 / 档位 / 库存四项全部走
  真实 API，不再回落到配置里那一对 `country` / `target_price`（实测 1 国 1 档 →
  183 国 923 档）。
- **环境账本** —— 给出口与指纹加带 TTL 的活租约，修掉「轮转池把同一出口分给两个活任务」
  的盲区。复用不隐藏：`stats()["reused_live"]` 直接回答「有多少出口被两个以上活任务共享」。
- **`upsert_account` 列级 merge** —— 空值不再覆盖非空。
- **供应商中立化** —— `SmsBower*` → `SmsProvider*`（Python + C# + 测试 + 文档），
  静态号池模式 A+B+C 全部退役，号码唯一来源改为 `phone_reuse` 租号池。
- **优惠状态栏「支付资格未知」三态标记** —— 区分「从未探测」（空后缀）/「探测到方式」
  （`card` / `upi` / `momo`）/「探测但未枚举出方式」（支付资格未知）。Checkout 被风控拒
  时不再把平台侧封锁读成账号属性。
- **`--doctor` 密钥来源与撞车检查** —— 两家以上解析后的 `api_key` 逐字相同时点名 section，
  不打印密钥、不发请求；同时补齐三家接码供应商的配置骨架。
- **新建 `docs/TROUBLESHOOTING.md`** —— 故障 → 编号检查清单，按顺序可执行，并指向负责
  该行为的源码位置。
- **对标落地的四项机制** —— 邮箱池 OAuth RT 回写、截图配 DOM 真值（真值先于截图采集并
  显式声明）、浏览器 profile 占用回收（`parent.lock` 不是证据）、未捕获异常两级钩子。
- **扫描落地与门禁** —— 删 `account_recovery` 兼容壳、实删 10 个零引用定义、补齐未文档化
  模块探针 13 → 0；新增 `unused_import` / `bare_print` / `config_key` / `delayed_import` /
  `mailbox_private_import` 五个 ratchet（按**逐文件**基线冻结存量、只禁增长），
  `docs_consistency_scan` 补「文档提到的 `.cs` 必须存在」检查。

## 修复问题

- **接码供应商密钥跨 section 写串** —— 设置界面「下拉框与输入框是两个独立控件」，改选
  供应商后没有任何东西重新解析，于是把上一家的 key 写进了新 section（表现为 HeroSMS
  `401` / Grizzly `NOKEY` / NeXSMS `403` 三连，而 smsbower 一直正常）。改为订阅 provider
  字段，下拉一变就重载 ⇒ 输入框显示的永远是这次保存真正会写进去的值。
- **`--phone-register` 把客户端类写死** —— 接入 nexsms 后拿 sms-activate 的 handler 路径
  去打 `api.nexsms.net`，稳定 404。改为 `phone_reuse.rental_protocol` 单一决策点：
  判定只写一处、接缝保持不动。
- **桌面端「一键接码」对 `nexsms_json` 走完全离线的 `else` 分支** ⇒ 直接 403。
  改为按协议族三元分派。
- **目录解析只认一种响应形态、价格接口没有回退**，且目录失败会直接终结「一键接码」。
  失败原因改为分两处：无路可退 → 弹窗，有路可退 → 日志。
- **配置分片写入器两侧不对齐** —— C# 缺原子写 / `.bak` / fsync，非 ASCII 被
  `JavaScriptEncoder` 转义成 `\uXXXX`（与 Python `ensure_ascii=False` 冲突），空片直接
  删文件（三片删光会把应用交回 legacy 单文件分支，复活刚删掉的键）。已对齐并新增按内容
  变化裁剪；配套修 `default_config_path().parent` 在根 `config.json` 删除后报错路径。
- **环境账本释放的「先读后写」竞态** —— 两个线程都读到 `active_holders=2`、都算出 1，
  `state` 永远停在 `leased`、`released_at=0`，僵尸租约白占出口整个 TTL 且 `reused_live`
  虚高。改为单条原子 UPDATE，让「减一」与「减到零了吗」成为同一个原子动作。
- **`config_usage` 报告在空输入时吞掉排除说明** —— 只在 CI 显形（CI 的 `config.json` 是
  example 副本、三个分片未被跟踪 ⇒ 未读键为空）。排除段改为无条件输出；同批修掉未读键
  假阳性 61 → 60、数据键守卫从静默通过改成大声跳过。
- **`refresh_doc_symbol_lines.py` 缺 `newline="\n"`** —— 在 Windows 上把被改写的文档整体
  翻成 CRLF（实测 279/279 行），而四道门禁全绿（行尾守卫只拒绝「混用」，`git diff` 被
  `core.autocrlf` 归一化）。唯一判据是 `git ls-files --eol`。
- **`scan_hardcoded_secrets.py` 三类整类漏检** —— 重写发布前扫描；对标文档里 16 处第三方
  真实明文密码脱敏为等价人造值。
- **测试与文档的静默失效** —— 三处 docstring 写在函数体首位之后（`__doc__` 恒为 `None`）、
  抽取后失效的 patch 面与两处扫描器误报、桌面冒烟测试与注册对话框签名脱节。
- **测试夹具写入现役真实凭据** —— 环境账本夹具原把真实代理出口账号 / 口令写成测试常量，
  已改为合成占位值。⚠️ 旧值仍在公开 git 历史里，**需在供应商侧轮换该出口凭据**。
