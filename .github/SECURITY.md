# 安全策略

## 支持范围

只有**最新发布标签**（`vYYYY.MM.DD`，见
[Releases](https://github.com/2951461586/GPT-Register-Tool/releases)）接受安全修复。
`dist/` 里的历史安装包不再回补；升级到最新标签即可获得修复。

## 报告漏洞

**不要开公开 issue。** 请用 GitHub 的私密通道：

- [Security advisory（推荐）](https://github.com/2951461586/GPT-Register-Tool/security/advisories/new)
- 或者邮件到仓库所有者的 GitHub 公开邮箱（见 profile 页）。

请在报告里带上：

| 项 | 说明 |
| --- | --- |
| 版本 | `git describe --tags` 或 release 标签 |
| 组件 | `sms_tool/` 模块名 / `SmsWorkbench` 页面 / `scripts/` 脚本 |
| 影响 | 能读到什么、能改到什么（凭据？账号？发布产物？） |
| 复现 | 最小步骤；涉及配置时只说键名，**不要贴凭据** |
| 证据 | 日志片段请先去掉代理口令、API key、邮箱密码、refresh token |

72 小时内确认收到，修复或缓解方案会在下一个发布标签里说明（必要时先发
security advisory）。

## 本仓库的已知边界（不是漏洞）

以下都是**设计如此**，报告前请先看
[`docs/CONTEXT.md`](https://github.com/2951461586/GPT-Register-Tool/blob/main/docs/CONTEXT.md)
与 `docs/current/` 下的对应文档：

- **运行数据落在仓库外**：`%LOCALAPPDATA%\GPTRegisterTool\config` 里的
  `proxy.json` / `runtime.json` / `payment.json` / `mailbox_tokens.txt` 是你的本机文件，
  被 `.gitignore` 与发布载荷扫描双重排除。它们**不在**仓库里，也不随安装包发布。
- **安装包内含 CPython**：`runtime/python` 是 `scripts/stage_python_runtime.ps1` 按
  `scripts/runtime_manifest.json` 的 SHA-256 校验后解出的固定版本，不是"随包的可执行文件"。
- **协议实现面对的是真实服务**：`sms_tool/auth_flow/`、`sentinel/`、`provider` 适配器按
  上游服务的行为改，行为变化（而非漏洞）请走普通 issue。

## 供应链

- 三个 workflow action 以**提交 SHA** 固定（`.github/workflows/ci.yml`），
  `sha_pinning_required` 已开启，只有 Dependabot 能移动它们。
- 依赖更新：`.github/dependabot.yml`（pip + 四个 NuGet 项目 + actions，每周）。
- Secret scanning 与 push protection 已开启；历史里一旦出现过凭据，按
  `docs/audits/` 的 rotate runbook 轮换，而不是重写历史。
