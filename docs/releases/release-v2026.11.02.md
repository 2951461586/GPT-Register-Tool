# v2026.11.02

自 v2026.10.28 以来的收口：修掉两个会让桌面端**用不了**的缺陷（声明了 mailbox lane 就开不了窗、
更新检查被 GitHub 限流后永久刷屏），把客户端指纹对齐到 10-31 的 HAR 实测值，
并给注册链路补上两个可 A/B 的开关。另有一次凭据暴露面清理。

## 新增功能

- **桌面直连出口收敛为一处** —— 桌面进程现在只有一个直连 HTTP 入口（GitHub release feed），
  host 白名单、lane 代理与「每请求一行日志」集中在一处；此前两个 `new HttpClient()` 各自为政，
  既不走代理也不留日志。顺带修好了一键接码的供应商目录与余额：它们不再由 WPF 自己发 HTTP
  （那条路曾是 703 行自带 host 与错误文案的实现），改由常驻后端通道返回，**API key 与 endpoint
  在 Python 侧解析，不再跨 IPC**。
- **两个注册 A/B 开关** —— `email_registration.auth_fingerprint.restore_high_entropy_hints`
  （默认 false）恢复被移除的五个高熵 `Sec-CH-UA-*` 头，用来给「不发高熵头」这件事补一个真正的
  对照臂；`registration.otp_validate_email_otp_validate_flow`（默认 false）把
  `email-otp/validate` 的 flow 名切成 `email_otp_validate`。两者都是单变量、预登记的实验臂，
  不开就与旧行为逐字节一致。

## 修复问题

- **声明了 mailbox lane 就直接开不了窗** —— 桌面端把 `proxy.lanes.mailbox`（对象）当字符串喂给
  `WebProxy`，`UriFormatException` 在构造器里抛出，`MainWindow` 解析失败，窗口根本出不来。
  改为读 `pool`、经统一的代理串归一化处理；坏值只丢代理不丢进程。
- **更新检查被 GitHub 限流后永久刷屏** —— 403 只记一行日志、不落盘，于是每次启动都白花一次配额
  再报同一句，手动点版本号还能绕过节流。现在读 `X-RateLimit-Reset` / `Retry-After` 把窗口写进
  `update_check.json`，窗口内启动与点击都不再请求，并显示「约 N 分钟后可重试」；`403` 按
  `X-RateLimit-Remaining` 区分「配额耗尽」（等待有效）与「出口被拒」（等待无效）；
  请求带 `If-None-Match`，`304` 不计配额。
- **测试与审计文档里泄露了真实代理池凭据** —— 公开仓库里有两组真实供应商凭据（一组自 09-13、
  一组自 08-06 起可检索）。已全部替换为合成模板，并给 pre-commit 守卫补上闸门：内容检查不再
  跳过 `docs/`、`tests/`，同时认识四段式 `host:port:user:pass`。**历史与 fork 无法回收，
  相关供应商口令仍需轮换**（细节见 `docs/audits/scans/scan-2026-11-02-proxy-pool-exposure.md`）。
- **客户端指纹与真实 Chrome 不一致** —— 每个 chrome 档位的 `sec_ch_ua` 改为按 curl_cffi 实测值
  生成（旧的品牌/顺序/GREASE 对所有版本都一样，没有任何 Chrome 长这样），
  `sec-ch-ua-full-version-list` 从档案自身派生而不是写死常量，`chrome150` 档位删除
  （curl_cffi 0.16.0 只到 146，选它只会在预检里失败或悄悄降级），chatgpt 家族补上
  `oai-language`。
- **注册结果键名漂移会静默显示全 0** —— Python 侧改名后 C# 读取方拿到默认值 0，界面当成
  「注册了 0 个」而不是报错。`result_schema.json` 现在把 `build_registration_batch_summary`
  的返回键与每个 C# 读取方钉住，改名必须在同一次提交里同步。
- **响应变形被误判成网络故障** —— 过宽的 `except Exception` 会把 `KeyError` 归到网络错误，
  掩盖缺陷并把失败塞进 unknown 桶，批次继续消耗邮箱。七处收窄到共享的传输错误类型，
  其余保留的宽 handler 都带上 `# broad-except: <理由>` 标记供复核。
