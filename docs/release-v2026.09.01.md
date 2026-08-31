# v2026.09.01

**这是一个安全重建版本，用于替换 v2026.08.31。**

> 🔴 **如果你下载过 v2026.08.31，请停止使用并删除。**
> 该版本的 zip 与 exe 内混入了本地诊断脚本 `scripts/pick_final_replacements.py`
> 与 5 个 `scripts/_diag_*.py`，其中含有 3 个凭据的 6 位前缀。
> 这些文件当时已从 git 历史中剔除并从工作区删除，但发布包构建于清理提交之前、
> 此后从未重建 —— git 历史干净，发布资产没有跟着干净。
> 相关资产已全部下架，本版本为清库后的完整重建。

## 本版本做了什么

### 1. 发布资产重建（替换 v2026.08.31）

`dist/release/` 下 v2026.08.31 的三个资产（exe / zip / sha256）已从 GitHub Release 移除。
v2026.09.01 的资产已重新校验，确认不含任何被 `.gitignore` 拒绝的文件，
也不含凭据类内容（PII 面：kyl_state / mailbox_tokens / 分片配置 / sessions 全部为 0 条）。

### 2. 发布流程加了闸门（此前完全没有）

`scripts/scan_release_payload.py`（新增）—— 在 payload 就绪、压缩打包之前执行，
不通过即中止构建。核心规则：**凡是 `.gitignore` 拒绝的文件，一律不许进发布包。**

payload 由 `git ls-files` 收集、却从工作树复制，被忽略但仍躺在磁盘上的文件
正是这样混进包的。本闸门用出事的那份 payload 做了验证：357 个源码类文件中
精准命中那 6 个，无假阳性。

`scripts/build_installer.ps1` 已在打包前接入该闸门。

### 3. 修掉两条形同虚设的 CI 凭据扫描

- `scan_hardcoded_secrets.py:10` 的 `ROOT` 套了 3 层 `dirname`，得到的是仓库根的
  **父目录**，于是 `SCAN_DIRS` 全部 `isdir` 失败被静默跳过；且脚本无 `sys.exit`，
  永远 exit 0 —— CI 上这一步是装饰品。已修正为 2 层，并补退出码、
  补「所有扫描目录都不存在时抛硬错误」的自检。
- 降噪：跳过 `site_key`（reCAPTCHA / hCaptcha 公钥，设计上公开）、
  `probe` / `placeholder` / `persistence` / `fallback` / `unauthorized` 等非凭据语义的
  变量，以及 `tests/`（测试必须构造假凭据，由 `test_precommit_guard.py` 兜底）。
  降噪后本仓 0 命中，自检确认真凭据仍会命中。

### 4. 发布版本号归一

此前 `-Version v2026.09.01` 会让 dotnet 报「不是有效的版本字符串」而构建失败，
不写 `v` 虽能构建但产物命名（`Setup-2026.09.01.exe`）与历史资产不一致。
两条路都是错的，已在参数入口统一归一。

## 升级说明

- 与 v2026.08.31 功能完全一致，仅替换发布资产并加固发布/扫描流程。
- 配置文件分片（proxy / runtime / payment）用法不变。
- 开发者：`python scripts/scan_release_payload.py dist/installer/package`
  可单独验证 payload；`python scripts/scan_hardcoded_secrets.py` 现在会返回真实退出码。

## 校验

下载后请核对 SHA-256（见同名 `.sha256.txt`）。
