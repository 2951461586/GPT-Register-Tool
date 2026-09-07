# v2026.08.31

距离上个版本 `v2026.08.22` 共 30 个提交。主线是三件事：**把 CI 修回全绿**、**把配置分片与凭据彻底移出版本库**、**无头浏览器注册的能力补齐与收敛**。

> ⚠️ 升级提示：配置文件已从单文件 `config.json` 拆分为 `proxy.json` / `runtime.json` / `payment.json` 三片。
> 桌面端与 Python 端在首次加载时会自动从旧 `config.json` 迁移，无需手工操作。
> 旧 `config.json` 保留在本地磁盘但不再优先读取，确认迁移无误后可自行删除。

## CI 修复（本次重点）

- `b05bd05` —— **进程探活误用 `os.kill(pid, 0)`，在 Windows 上等于向进程组广播 Ctrl+C**。
  `signal.CTRL_C_EVENT` 在 Windows 上就是 `0`，所以 `os.kill(pid, 0)` 并非"探活"，而是
  `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`。账号健康队列用自身 pid 做存活判断，
  在 CI runner（以 `CREATE_NEW_PROCESS_GROUP` 起步骤进程，当前 pid 同时是 pgid）上把
  Ctrl+C 投递回自己，整个 pytest 会话在第 13 个测试处 `KeyboardInterrupt` 中断。
  改为：自身 pid 短路 + Windows 走 `OpenProcess`/`GetExitCodeProcess` 判断
  `STILL_ACTIVE (259)` + 其他平台保留 `os.kill(pid, 0)`，并补 3 条回归测试。
- `290b58b` / `ce8dddb` —— `SmsWorkbench.Contracts` 的 NuGet lock 文件与 RID 集合不一致导致 NU1004。
  `dotnet test` 不带 RID 还原、发布脚本带 `-r win-x64` 还原，两者要求相反，只改 lock 文件必然二选一失败；
  最终在 csproj 里声明 `<RuntimeIdentifiers>win-x64</RuntimeIdentifiers>`，让两种还原都一致。
- `fea8132` —— sentinel vendored runtime 纳入版本库，CI 上不再缺失。

## 安全

- `31a1660` —— 配置分片（`proxy.json` / `runtime.json` / `payment.json`）移出版本控制并补 `.gitignore`。
  这三片此前随提交进了版本库，里面带着明文凭据字段。
- `4d67404` —— 诊断脚本里的硬编码 token / 代理口令改为从环境变量读取。

## 无头浏览器注册

- `6cb7427` —— 无头浏览器注册改造：脉冲调度（`registration.pulse`，默认开）、
  浏览器进程池（`registration.browser_process_pool`，默认 `max_concurrent: 4`）、
  新增 ADSPower 驱动、账号↔代理槽固定绑定（重试不再偏移出口）。
- `8296290` —— 指纹浏览器注册风控补强 P1–P4（对照 turb-gpt-free-register / aBaiFreeGPT）。
- `0b4de3f` —— Roxy 收敛为单一 CDP 实现。
- `32702f0` / `37928f5` —— Roxy `close()` 删除 profile 加重试退避、会话 `__enter__` 失败时的
  profile 泄漏修复，根治关窗竞态残留孤儿。
- `f43fbca` —— 移除 Stalwart 邮箱适配器。

## 模块瘦身与重构

- `4c43753` / `b028d23` —— 移除 WebUI 与 WebHost 模块。
- `fdf2368` —— `config.json` 拆分为 proxy / runtime / payment 三片，C# 与 Python 两端合并读取逻辑对齐。
- `a559b9c` —— `storage.py` 拆分下沉到 `sms_tool/store/` 子包（6 层 + 薄壳兼容），
  修复 `cli.py` 懒加载回归。
- `5ec2850` —— `payment_link_manager.py` 拆分下沉到 `sms_tool/pay_link/` 子包（7 层 + 薄壳兼容）。
- `a988bc8` —— `gen_pp_link.py` + `paypal_reconciliation.py` 拆到 `sms_tool/paypal_link/` 子包。
- `bc9e9df` —— `paypal_auto.py` 拆为 `sms_tool/paypal/` 分层包。

## 缺陷修复

- `531e1a6` —— 账号删除时支持 `@` / `+@` 别名邮箱的模糊匹配。
- `932eb14` —— camoufox 驱动启动后 `new_page()` 永久挂起（Firefox content 沙箱）。
- `adfeb94` —— 优惠检测永远返回 HTTP 0（`browser_fetch` 的 status 键名未归一化）。
- `a04fb27` / `229afc9` —— 代理列表文本统一为平台无关的 LF。
- `0fca46a` —— 修正 `.gitignore` 过宽规则导致 `__init__.py` 被忽略。
- `bfa96ad` —— WPF 下拉弹窗布局的测试等待。

## 验证

- Python 全量测试：**1264 passed**（另有 60 个 subtests），Python 3.11 与 3.12 双版本均通过。
- .NET 测试：**220 passed**。
- GitHub Actions run `33323128048`：12 个步骤全绿，含 `pytest -q`、`dotnet test`、
  `sensitive_field_scan`、`architecture_scan` 与 `build_dotnet.ps1`。
- 发布产物 SHA-256 已复算并与清单逐字节比对一致；zip 包内 408 个条目扫描无真实凭据文件。
