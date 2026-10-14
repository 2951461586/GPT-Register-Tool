# 对标扫描：`cxqc168-wq/gpt-register-pro`

日期：2026-09-22
扫描对象：https://github.com/cxqc168-wq/gpt-register-pro
扫描方式：全量 clone（`--depth 1`，单提交 `7b2304f`，2026-09-19），逐模块读源码取证
对照基线：本仓 `F:\epsoft\GPT-Register-Tool`（HEAD 与工作区实际状态）

---

## 0. 结论先行

1. **许可证与上次对标同一约束**：对方 **AGPL-3.0**（`LICENSE` 文件），本仓**无 LICENSE** ⇒ **只能借鉴机制，不能逐字搬代码**。注意对方 `package.json` 写的是 `"license": "ISC"`，**与 LICENSE 文件冲突，以 LICENSE 文件为准**。
2. **本次最高价值产出不是"能抄什么"，而是两条本仓真实缺口的独立证据**：
   - 🔴 **邮箱池的 OAuth `refresh_token` 轮换后不回写**（本仓 `providers/mailbox_graph.py:47-48` 只改内存，全仓 **0 条**落盘路径）；对方 `使用说明.md §12.4` 明写"自动回写到账号池，避免旧 Token 失效"。
   - 🔴 **本仓短信取消全部是同步阻塞调用**（`phone_registration.py` 5 处 `sms_client.cancel()`），**没有**延迟取消 + 退出前排空机制；对方有专门的 `src/deferredCancelManager.js` 保障退款不遗漏。
3. **对方的密钥正在公开仓库里泄露**（4 个文件、三家接码平台真实 API Key + 真实账号密码 + 授权服务器明文 IP）。这条对我们是**反面教材**，且恰好命中本仓 skill `release-channel-leak-response` 的那条判据。
4. **不要被对方的文档密度迷惑**：`docs/superpowers/plans|specs` 里 4 份方案文档都写了"测试"章节（含具体用例），但工程里 **`npm test` 恒定失败**、零测试框架、只有 3 个 ad-hoc 脚本。本仓 4703 测试是碾压性优势。

**建议动作**（详见 §3）：
- 立即可做、零许可证风险：**② RT 回写**、③ 卡密预校验、⑤ 截图配 DOM 真值、⑩ 排错文档结构。
- 需老板拍板：④ 延迟取消（涉及"钱"和主流程改造）、⑥ 供应商层收口、⑦ 配置真源收口。
- 不做：`writeLock`（本仓是 SQLite 事务模型）、`licenseGuard`（整体失效的授权体系）、任何代码搬运。

---

## 1. 项目画像（客观对比）

| 维度 | 对方 `gpt-register-pro` | 本仓 `GPT-Register-Tool` |
|---|---|---|
| 语言/运行时 | Node.js ≥18 + Electron 42 | Python 3.13（主）+ C#/WPF（桌面）+ 少量 JS |
| 规模 | 57 文件 / ~12.4k 行 JS（`index.js` 单文件 103KB、`browserService.js` 117KB） | 远大于此（`sms_tool/` + `services/` + `tests/`） |
| 注册路线 | **浏览器自动化为主**（`puppeteer-real-browser` + 真实 Chrome，靠真浏览器过 Turnstile） | **纯协议为主**，`camoufox`/`roxy` 浏览器仅作兜底 |
| 接码 | 3 家（HeroSMS / Grizzly / NexSMS），统一接口 + 注册表 | `sms_tool/` 自有供应商层 |
| 邮箱 | Cloudflare Worker（自建零成本）+ cloud-mail + Outlook 池（Graph/IMAP 双模式） | 已有 **cfworker**（`providers/mailbox_cfworker.py` + `cfworker_client.py`）+ gmail/graph/icloud_url/remail/smailr ⇒ **自建邮箱这条本仓已存在，不构成借鉴点** |
| OAuth | 浏览器走 OpenAI OAuth PKCE，本地 `localhost:1455/auth/callback` 收 code | 纯协议 sentinel 链路 |
| 测试 | **零**（`npm test` = `echo "Error: no test specified" && exit 1`） | 4703 passed / 6 skipped（09-22） |
| 提交历史 | 单提交（2026-09-19） | 长期迭代，有并行提交者 |
| 许可证 | AGPL-3.0（`package.json` 误写 ISC） | 无 LICENSE |

**定位判断**：这是一个**体量约为本仓 1/10、工程成熟度明显更低**的同类项目。它的价值集中在**若干独立机制的设计细节**上，而不是整体架构。

---

## 2. 🔴 P0 安全发现：对方的公开仓库正在泄露真实凭据

**这不是"借鉴项"，是"引以为戒 + 可选的善意提醒"。**

### 2.1 泄露清单（已取证，均为工作区当前内容）

| 文件:行 | 泄露内容 |
|---|---|
| `desktop/visual-qa-capture.js:73,75,77` 及 `:110,112,114` | HeroSMS / NexSMS / Grizzly SMS **真实 API Key 各一份**（写死在视觉 QA 脚本的 mock 数据里） |
| `desktop/visual-qa-preload.js:8,10,12` | **同上三份 Key**（第二处副本） |
| `docs/superpowers/specs/2026-09-15-grizzly-sms-provider-design.md:11` | Grizzly API Key（设计文档正文里） |
| `docs/superpowers/specs/2026-09-15-nexsms-provider-design.md:85` | NexSMS API Key（配置示例里） |
| `batch-oauth.js:24-28` | **3 个真实账号**的手机号 + 明文密码 + 姓名 + 出生日期 |
| `finish-oauth-no-email.js:19-33` | 真实手机号 + 明文密码 + `SMS_ACTIVATION_ID = 854451613` + 目标邮箱 |
| `src/licenseGuard.js:7` | 授权服务器地址 **明文 HTTP**：`http://64.90.20.244:8443` |
| `src/licenseGuard.js:8` | 期望激活码硬编码：`EXPECTED_CODE = 'CXQC0168'`（`desktop/renderer/index.html:488` 的 placeholder 里也出现） |

### 2.2 为什么 `.gitignore` 没挡住

对方 `.gitignore` **确实配了** `config.json` / `config.local.json` / `config.server.json` / `outlook-accounts.json` 等忽略项，`git check-ignore -v desktop/visual-qa-capture.js` 返回 **exit=1（未被忽略）**——它本来就该被提交（是源码）。

⇒ **密钥不在被忽略的文件里，而在源码里**，所以"忽略清单"这条闸门对它完全无效。

**这正是本仓 skill `release-channel-leak-response` 已有的判据**："发布闸门用 .gitignore 当白名单，但这条规则的前提必须单独审——git 说没忽略，不等于没泄露"。本次是这条判据的一个干净实例。

### 2.3 处置建议

- **本仓不引用、不验证这些凭据**。用别人的 API Key 发请求属未授权使用，本次扫描**刻意没有做任何请求验证**。
- 是否善意提醒作者（开 issue / 邮件）是**外部动作**，需老板同意后再做。
- 对本仓的直接价值：把"源码内联密钥"补进我们的发布前扫描清单（我们现在扫的是 `.gitignore` 覆盖面和 Release 资产，需确认是否也扫源码内联形态）。

---

## 3. ✅ 值得借鉴（按价值排序，均已取证）

### ② 【最高价值】OAuth `refresh_token` 轮换后回写账号池

**对方做法**：`src/outlookProvider.js:371-375`
```js
// OAuth 轮换出新 refresh_token 时回写存储，避免旧 token 失效
const rotated = data?.refresh_token;
if (typeof rotated === 'string' && rotated.trim() && rotated !== account.refreshToken) {
    account.refreshToken = rotated.trim();
    // ...持久化
}
```
`使用说明.md §12.4` 明写："OAuth 刷新时如果返回新的 refresh_token，会自动回写到账号池，避免旧 Token 失效。"

**本仓现状（已取证）**：`sms_tool/providers/mailbox_graph.py:47-48`
```python
if body.get("refresh_token"):
    mailbox.refresh_token = body["refresh_token"]   # 只改内存对象
mailbox.access_token = access_token
```
- 全仓搜索"refresh_token + 落盘动词（`write_text` / `open(` / `json.dump` / `_save` / `persist`）"⇒ **命中 0 条**。
- `sms_tool/mailbox.py:440` 只有 `mailbox_tokens.txt` 的**路径常量**，没有写回逻辑。

**后果**：池子里存的永远是**首次导入时的旧 RT**。Microsoft 一旦轮换（Graph 刷新常返回新 RT），旧值即失效 ⇒ 下次取件直接 `invalid_grant`，而我们在账本上只会看到"取件失败"，**看不出是 RT 陈旧**。

**🔴 与本仓已知终态的关系（谨慎表述）**：本仓待办记着"96 终态 = 55 域名 404 + 30 应用密码失效"。这 30 条是 **`app_password` 认证模式**（`mailbox_parsers.py:167-210`），与 OAuth RT 轮换**不是同一类**。所以本项**不能解释**那 30 条，但它是一条**尚未被记账的独立失败源**——凡走 `oauth_refresh` 的邮箱都会中招，且症状会被归到"取件失败"里。

**落地注意（踩过的坑，必须先看）**：
- 本仓铁律：**非 Python 生成的池文件必须字节级精确文本替换**（`proxy.json` 类的教训）。`mailbox_tokens.txt` 是人工/供应商导入的，**不要用 Python 重写整个文件**（`Path.write_text()` 在 Windows 会翻 CRLF）。
- 另一条铁律：账本**不是全量、按 `account_ref` 键控**，`record()` 的 `else` 分支会 `pop` 整行 ⇒ 回写路径不能假设"查不到 = 没发生"。
- 因此建议**先拍板粒度**：是回写池文件（字节级替换），还是先补 `accounts` 侧的 `mailbox_refresh_token` 列（记忆里明确记着"它不在兜底表里，不是 `accounts` 的列"）。

---

### ③ 卡密/邮箱行导入时的"占位符预校验"

**对方做法**：`src/outlookProvider.js:64-84` 与 `:110-114`
```js
// 真实 Microsoft refresh_token 长度通常在 800 字符以上，低于该下限必然不是真实凭据
function isPlaceholderRefreshToken(token) { /* 长度 + 模板值匹配 */ }
// ...
result.errors.push(`第 ${lineNo} 行 ${normalized}：refresh_token 疑似占位符/模板值（长度 ${refreshToken.length}），请填入卡密中的真实 refresh_token（通常 800 字符以上）`);
```
- 逐行报错、带行号、带**实测长度**，且**拒绝入库**（不是静默跳过）。
- `使用说明.md §14.6` 把这条做成了独立排错条目。

**本仓现状**：`sms_tool/mailbox_parsers.py:167-210` 是 `if not app_password: print("[!] Skip malformed ..."); continue` 形态——**静默丢行 + 无长度/形态判据**。

**价值**：这与上次对标 `gpt-outlook-register` 的第 5 条（"导入全对才写"）**同源**，但多了一个**可直接用的域内启发式**（RT 长度下限 + 占位符形态）。能提前拦住"供应商给了模板卡密"这一类，避免把注定失败的邮箱写进池子。

---

### ④ 延迟取消（deferred cancel）+ 退出前排空

**对方做法**：`src/deferredCancelManager.js`（全文 62 行，无外部依赖）

问题背景（`docs/superpowers/specs/2026-09-16-deferred-cancel-and-concurrency-design.md`）：
> Grizzly 的 `cancel()` 在号码创建不足 2 分钟时会收到 `EARLY_CANCEL_DENIED`，并每 15s 轮询重试直到取消失败或超过 5 分钟窗口 ⇒ **主流程被阻塞近 2 分钟**。

机制要点：
- `schedule(provider, { readyAtMs, phone })` **立即返回、不阻塞**，后台等到 `readyAtMs` 再调 `cancel()`。
- `_inflight` 集合**防重复取消**（同一个 provider 不被取消两次）。
- `cancel()` 异常**吞掉 + warn**，理由是"号码到期后自动退款"兜底。
- `flush()`：进程退出前**等所有排队取消排空**（含尚未到点的，先等到点再取消）——**这是"退款不遗漏"的保险丝**。
- 定时器**刻意不 unref**，保持进程存活。

**本仓现状（已取证）**：取消全是同步调用 —— `sms_tool/phone_registration.py:153,192,202,231,286` 与 `phone_reuse.py:550,692,699`。
🔴 **注意别混淆**：本仓 `batch_runner.py:330-348` 与 `commands/registration.py:152-219` 里也有 `deferred` 这个名字，但语义是**邮箱冷却跳过**，与"延迟取消"**完全不同**。

**价值**：这是**"钱"的机制**——把"退款保障"从关键路径上摘下来，同时保证不丢。是否适用取决于我们的接码供应商是否也有"早期取消拒绝"的窗口期；**需先实测我们的供应商行为**再决定，不要照搬 2 分钟这个常数。

**附带资产**：那份方案文档的结构（背景与问题 → 目标 → 架构与改动 → 错误处理 → 测试 → 风险与注意点）写得干净、每处改动都带 `file#L` 引用，**可作我们写方案文档的模板**。

---

### ⑤ 截图必须配 DOM 真值（防"像素滞后"误判）

**对方做法**：`desktop/visual-qa-capture.js:13-24, 29-38`
```js
// RDP + 禁用硬件加速时 capturePage 可能持续返回旧合成帧；CDP 截图由渲染器侧强制出图，优先使用
try {
  if (!win.webContents.debugger.isAttached()) win.webContents.debugger.attach('1.3');
  const shot = await win.webContents.debugger.sendCommand('Page.captureScreenshot', { format: 'png' });
  ...
} catch { /* 回退 capturePage */ }

// 截图之外输出 DOM 真值：确认服务商切换在 DOM 层已生效（避免像素滞后误判 UI bug）
async function logSmsDomState(win, label) { /* 打印 activeBtn/chip/updated/balance */ }
```
另有 `app.disableHardwareAcceleration()`（第 41 行）解决远程桌面下 `capturePage` 报 `UnknownVizError`。

**价值**：**"不要用像素判断状态，要用 DOM/产物判断"** —— 与本仓"按产物判成败、不按状态码"（`http_utils._cookie_presence` + `account_creation.py` 的 `nextauth_session` 判据）是**同一条文化**，可直接用在我们的浏览器兜底流程与探针脚本上。他们连"截图失败要回退"和"回退后仍要取 DOM 真值"都写清楚了，这个双通道做法值得照搬思路。

---

### ⑥ 多供应商注册表（新增供应商 = 1 文件 + 1 注册项 + 1 UI 卡片）

**对方做法**：`src/smsProviderFactory.js`（全文 84 行）
```js
const SMS_PROVIDER_REGISTRY = {
  herosms: { label: 'HeroSMS', Class: SMSProvider,        apiKeyField: 'heroSmsApiKey', serviceField: 'heroSmsService' },
  nexsms:  { label: 'NexSMS',  Class: NexSmsProvider,     apiKeyField: 'nexSmsApiKey',  serviceField: 'nexSmsService'  },
  grizzly: { label: 'Grizzly SMS', Class: GrizzlySmsProvider, apiKeyField: 'grizzlySmsApiKey', serviceField: 'grizzlySmsService' },
};
```
- 一张表把 `type / label / Class / apiKeyField / serviceField` 收全 ⇒ **改动面被钉死**，`getActiveSmsProviderType()` 对非法值**显式回落默认**而不是抛错。
- `grizzlySmsProvider.js` 用**继承 + V1/V2 协议自动降级**复用 HeroSMS 的 sms-activate 协议，只覆写差异部分。

**价值**：可作为我们 `sms_tool/` 供应商层的**改动成本基准**——若本仓新增一家供应商需要改的地方明显多于"1 文件 + 1 条注册项"，就值得收口。**这条需要先量本仓现状**（本次未做，属待评估）。

---

### ⑦ 配置分层：base + profile + env，优先级显式且只有两层真源

**对方做法**：`src/config.js:22-42, 90-107`
```
config.json（基础） → config.<profile>.json（平台覆盖：darwin→local / linux→server）
                    → 环境变量代理兜底（HTTPS_PROXY > HTTP_PROXY > ALL_PROXY，大小写各试）
显式指定：CONFIG_FILE=<path> 或 CONFIG_PROFILE=<name>
```
合并语义就是一行 `{...baseConfig, ...profileConfig}`，**覆盖优先级一目了然**；文件缺失时打 `[Config] 未找到可用配置文件` 并继续（不静默）。

**对照本仓痛点**：我们有三处真源（`registration.*` → `runtime.json`；代理池 → `proxy.json`；`config.json` 有分片即死文件），且"`config.json` 分片即死文件"本身就是一条已记录的坑。

**价值**：他们的模型很朴素，但**"只有一层基础 + 一层显式覆盖"**这个约束比我们现在的三处真源干净。**不是让我们照搬两层结构，而是提示：真源数量本身就是设计决策**，我们的三处真源应作为"配置收敛"待办的输入。

🔴 **顺带发现他们自己的不一致**：`config.js:146` 的 `mailProvider` 兜底是 `'cloud-mail'`，而 `config.example.json` 与 README 推荐的是 `'cloudflare-worker'` ⇒ **缺省路径 ≠ 文档路径**。与本仓"配置真源分散"同型，属引以为戒。

---

### ⑧ 运行日志：console tee + 进程级未捕获异常兜底

**对方做法**：`src/runLogger.js`
- 劫持 `console.log/info/warn/error` **双写**（文件 + 原 stdout），`util.formatWithOptions({colors:false, depth:8})` 保证文件里可读。
- `process.on('uncaughtException' / 'unhandledRejection')` 都写 `FATAL` 行（`:62-67`）。
- 日志文件按 `run-YYYYMMDD-HHMMSS.log` 命名。

**价值**：本仓已有结构化日志与观测元数据，整体价值一般。但**"未捕获异常也必须进同一份运行日志"**这条判据值得核对——否则崩溃点会只出现在 stderr 而漏出运行日志。属**低成本核对项**。

---

### ⑩ 排错文档结构：故障 → 编号检查清单

**对方做法**：`使用说明.md §14`，每条故障给**按顺序可执行**的检查步骤。例：
```
### 14.3 收不到邮箱验证码
按顺序检查：
1. Cloudflare Email Routing 是否启用
2. MX 记录是否正确
3. Catch-all 是否设置为 Send to Worker
4. Worker 是否绑定 D1（绑定名必须是 `DB`）
5. Worker 变量 `MAIL_DOMAIN` 是否是你的域名
6. `/api/health` 是否返回 {"ok":true}
```
另有 §14.6（占位符 RT）、§14.5（切换供应商不生效：确认字段取值 + Key 已填）等。

**价值**：把叙述式排错改成**编号清单**，读者不需要读完段落就能照着做。

**🔴 2026-09-22 执行时的修正**：本节原先写「本仓 `docs/` 里的排错内容偏叙述」，
实测后这句**说轻了** —— 本仓**根本没有排错文档**。`docs/` 下全是架构与契约文档，
`README.md`（671 行）里也没有任何「出问题按顺序查这几条」的段落。所以 ③ 不是
「改写结构」，而是**从零新建**：`docs/TROUBLESHOOTING.md`（11 条故障清单，
每条按顺序可执行，并指向负责该行为的 `file.py:line`）。

---

## 4. 🚫 不要抄 / 引以为戒（对方自己的坑，均已取证）

### 4.1 `writeLock` 不可重入，且是纯进程内锁
`src/writeLock.js`（27 行）按文件路径分组的 Promise 队列：
```js
async function withFileLock(filePath, fn) {
    const prev = locks.get(filePath) || Promise.resolve();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    locks.set(filePath, gate);
    await prev;            // ← 若 fn 内再 withFileLock(同一路径)，这里永远等不到
    try { return await fn(); } finally { release(); }
}
```
- 🔴 **不可重入**：同一路径嵌套加锁 ⇒ `prev` 是外层未 resolve 的 gate ⇒ **死锁**。
- 🔴 **只在进程内生效**：他们的 Electron 主进程 + CLI 子进程是并存的，跨进程完全不设防。
- **本仓不需要**：我们的并发模型是 SQLite 事务（且有"别切 autocommit"的明确契约，见 `tests/test_store_transaction.py` 的模块 docstring）。记下来只为**以后看到类似"键控异步互斥"方案时知道它的两个边界**。

### 4.2 `licenseGuard`：一整套失效的授权体系
`src/licenseGuard.js` 设计上不算差（RSA-SHA256 签名凭证 + 机器绑定走 Windows `MachineGuid` → MAC → hostname 三级降级 + **时钟回拨检测** `now < last_seen`），但：
- 🔴 `getLicenseStatus(isPackaged = true)`（`:251-258`）：**未打包时无条件返回 `OK`**，消息是"开发环境已放行（未打包）"⇒ **fail-open 的安全闸门**。
- 🔴 源码是 AGPL 公开的 ⇒ 任何人删掉调用点即可。
- 🔴 授权服务器走**明文 HTTP**（`http://64.90.20.244:8443`）且 IP 硬编码。
- 🔴 期望激活码 `CXQC0168` **直接写在源码里**，连 `desktop/renderer/index.html:488` 的 placeholder 都用了它。
- 更根本的矛盾：**AGPL-3.0 + 商业化授权锁**天然互斥。

**教训（通用）**：安全/授权闸门的默认行为必须是 **fail-closed**；"开发环境放行"这类分支必须有显式的编译期隔离，否则会随源码一起流出去。

### 4.3 零测试，但方案文档都写了测试
- `package.json`：`"test": "echo \"Error: no test specified\" && exit 1"`。
- `使用说明.md §14.7` 明说："当前项目没有测试脚本，`npm test` 本来就会报错。检查语法用 `node --check ...`"。
- 但 `docs/superpowers/plans/2026-09-16-*.md` 的"测试"章节写了很具体的用例（"构造 `PHONE_ALREADY_REGISTERED`，确认主流程立即返回……且约 2 分钟后 `cancel()` 成功退款"）。
- 仓库里只有 3 个 ad-hoc 脚本（`scripts/test-concurrent-launch.js` 等），靠 `process.exit(1)` 报失败，**无断言库、无 runner**。

⇒ **"方案有测试设计 ≠ 工程有测试执行"**。这是本次扫描最容易误判的地方：文档密度高会让人高估其成熟度。本仓 4703 测试（含 842 subtests）是结构性优势，不要被对方的文档说服去"补齐文档"。

### 4.4 仓库卫生：一次性调试脚本直接留在根目录
| 文件 | 内容 |
|---|---|
| `check-pool.js` | 硬编码 3 个菲律宾手机号，打印特定账号 |
| `mark-dead.js` | 硬编码 3 个手机号 + 把状态改成 `'废弃'` + 写回 `accounts.json` |
| `batch-oauth.js` | 硬编码 3 个账号的手机号/密码/姓名/生日/国家 |
| `finish-oauth-no-email.js` | 硬编码 1 个账号 + `SMS_ACTIVATION_ID` + 目标邮箱 |
| `diag-phone-page.js` | 一次性页面诊断（注释自称"不提交、不买号"，但**已提交**） |

**本仓有对应 skill**：`repo-hygiene-and-push-verification`（"删之前必须读生成它的那段源码"）。这批文件是"**调试产物直接进仓库**"的典型，且其中 3 个含真实凭据。

🔴 **特别注意 `finish-oauth-no-email.js` 的文件名有误导性**：它**并非**"无需邮箱即可完成 OAuth"，而是"该脚本硬编码了一个特定账号 + 一个指定邮箱"的一次性恢复脚本。**不要因为文件名就以为存在"免邮箱换 token"的捷径**——这条如果误读，会误导邮箱池策略。

### 4.5 其他
- `package.json` 的 `license: ISC` 与 `LICENSE`（AGPL-3.0）冲突 ⇒ **引用任何代码前以 LICENSE 文件为准**。
- `index.js` 单文件 103KB、`browserService.js` 117KB、`renderer/app.js` 52KB ⇒ 巨型单文件结构（本仓有 `python-module-split-refactor` skill 处理同类问题）。
- `randomIdentity.js` 的姓名池（约 260 个名字，明显取自某游戏角色名）**远大于**常用假名池，但**只用于 name 字段**，不参与任何指纹/画像 ⇒ 与我们的画像维度不是一回事，无借鉴价值。

---

## 5. 明确"本仓已有、无需动"的项

| 能力 | 对方 | 本仓 |
|---|---|---|
| 自建零成本邮箱（CF Worker + D1 + Email Routing catch-all） | `cloudflare-email-worker.js` + `cloudflare-email-worker-schema.sql` | **已有**：`providers/mailbox_cfworker.py` + `cfworker_client.py`（池里已有 `cfworker://` 行与自有域名） |
| Graph / IMAP 双模式取件 + scope 回退 | `outlookProvider.js`（Graph → IMAP XOAUTH2） | **已有**：`mailbox.py:880-956` `_fetch_mailbox_messages_local` 先 Graph 再 IMAP，含 `scope_override` |
| 发码前先取时间戳 | `index.js` `phaseStartedAt` + `minTimestampMs` | **已有**（且更严：`issued_after_unix`） |
| 传输瞬断在原 session 重试 | `_graphGet` 3 次重试 | **已有且更强**（同 session + `Connection: close` 强制新连接） |
| 失败记录与恢复入口 | `shibai.json` + `--phase2/--phase3/--phase8` | **已有且更细**（failure_registry 精确率/召回率纪律、`--retry-terminal`） |
| 浏览器残留进程清理 | `browserService.cleanupStaleChrome()`（`taskkill /PID /T /F`） | **待核对**：本仓 `browser_session.py` 有 deterministic cleanup 与 isolated context，但未见按 profile 锁反查残留进程的逻辑。**属低成本核对项，未取证结论** |

---

## 6. 行动清单（按可执行性排序）

| # | 项 | 成本 | 许可证风险 | 状态 |
|---|---|---|---|---|
| ① | **邮箱池 OAuth RT 回写**（本仓确认缺失） | 中（涉及池文件写入，受"字节级精确替换"铁律约束） | 无 | ✅ **已落地** `sms_tool/mailbox_pool_writer.py` + 27 测试 |
| ② | 卡密导入占位符预校验（RT 长度 + 形态） | 低 | 无 | ✅ **已落地** `mailbox_parsers.py` 三处接线 + 21 测试 |
| ③ | 排错文档改编号清单（§14 结构） | 低 | 无 | ✅ **已落地** `docs/TROUBLESHOOTING.md`（新建，非改写） |
| ④ | 截图配 DOM 真值（浏览器兜底/探针） | 低 | 无 | ✅ **已落地** `sms_tool/page_truth.py` + 14 测试 |
| ⑤ | 核对 `uncaughtException` 是否进运行日志 | 极低 | 无 | ✅ **核对完成且已补**：原本**不进**，现装进程/线程两级钩子 + 8 测试 |
| ⑥ | 核对浏览器残留进程清理 | 低 | 无 | ✅ **核对完成且已补**：原本只能拿到不含持有者的报错，现指名进程 + 23 测试 |
| ⑦ | 延迟取消 + 退出前排空 | 中高（先实测供应商是否有早期取消窗口） | 无 | ⛔ **前置条件实测为「不可判」** ⇒ 只补观测，不实现（见 §7.2） |
| ⑧ | 供应商层改动成本量测（对照"1 文件 + 1 注册项"） | 低 | 无 | ✅ **已量测**：Python 侧达标，C# 侧是未受守卫的漂移面 ⇒ 已补平价测试（见 §7.1） |
| ⑨ | 配置真源收敛（三处 → 一层基础 + 一层覆盖） | 高 | 无 | ⛔ **建议不做**：测量显示 legacy 分支是 C# 侧承重墙（见 §7.3） |
| ⑩ | 发布前扫描补"源码内联密钥"形态 | 低 | 无 | ✅ **已落地**：`scan_hardcoded_secrets.py` 重写 + 51 测试（见 §7.4） |

**不做**：任何代码搬运（对方 AGPL-3.0 / 本仓无 LICENSE）、`writeLock`、`licenseGuard`。

---

## 7. 落地记录与量测结果（2026-09-22 执行）

### 7.1 ⑧ 供应商层改动成本：Python 侧达标，C# 侧无守卫

对照基线「新增供应商 = 1 provider file + 1 registry entry + 1 UI card」，以新增一个
**浏览器注册驱动**为样本实测：

| 层 | 位置 | 是否派生 | 改动量 |
| --- | --- | --- | --- |
| 词汇表 / 别名（真源） | `sms_tool/registration_drivers/base.py:53` `DRIVERS` | **真源** | 1 条 |
| 会话工厂表 | `sms_tool/registration_drivers/external_sessions/__init__.py:22` | 手写，**被 assert 钉在注册表上** | 1 条 |
| 会话实现（"1 provider file"） | `sms_tool/registration_drivers/external_sessions/managed.py` | — | 1 个类 |
| CLI choices | `sms_tool/cli_parsers/core.py:49` ← `driver_choices()` | ✅ 派生 | **0** |
| 配置校验 | `sms_tool/config.py:480` `sms_tool/config.py:503` ← `KNOWN_DRIVER_ALIASES` / `BROWSER_REGISTRATION_DRIVERS` | ✅ 派生 | **0** |
| 桌面端驱动枚举 | `SmsWorkbench/SettingsCatalog.cs:56` | ❌ **手写第二份** | 1 行（手写） |
| 桌面端各驱动配置卡片 | `SmsWorkbench/SettingsCatalog.cs:61` 起 | ❌ 手写 | N 行 |

**结论**：Python 侧已达到并超过基线（注册表 1 条 + 工厂 1 条，其余全部派生，且
`external_sessions/__init__.py:22` 的 `assert` 让「只加注册项不加工厂」在 **import 时**
就炸）。**唯一未受守卫的漂移面是 C# 的 `SettingsCatalog.cs`** ——
`scripts/config_key_baseline.json` 棘轮只管**键名**、不管**枚举选项值**。

两个方向的漂移后果不同，且都不轻：

- Python 加了驱动、C# 没加 ⇒ 桌面端下拉框里**选不到**该驱动（功能缺失，看得见）；
- C# 留了 Python 已删的驱动 ⇒ 桌面端**能选中**，但后端 `create_browser_session`
  抛 `unsupported_registration_driver`（配置界面在撒谎，只有运行时才发现）。

**已补**：`tests/test_settings_catalog_driver_parity.py`（7 测试）从 C# 源码里按括号配平
抽出驱动选项字面量，断言与 `DRIVERS` **双向相等**。已做变异验证：从 C# 列表删掉
`camoufox` 后测试变红并指名缺哪个。

### 7.2 ⑦ 延迟取消：前置条件「不可判」，因此不实现

报告要求先实测「本仓供应商是否存在早期取消拒绝窗口」。实测结论：**从当前代码无法判定，
且原因本身就是缺陷。**

链路是 `phone_reuse._cancel_smsbower_activation` → `SmsBowerClient.cancel` →
`set_status(id, "8")`，供应商的答复被**三重销毁**：

1. `smsbower.py` 的 `cancel()` 把**所有异常吞成 `False`**；
2. 它只比较 `== "ACCESS_CANCEL"`，**其余任何答复都不留痕**；
3. `phone_reuse.py:697` 的调用方**把返回值整个丢掉**（`_smsbower_client(slot).cancel(...)`）。

于是「因激活太新被拒」（sms-activate 协议族会回 `EARLY_CANCEL_DENIED`）与「网络挂了」
在每一份日志里长得完全一样。要回答这个问题就只能**花钱做专门的探测** —— 而这正是
"不可判" 的定义。

**处置**（按仓内方法学：不可判 ⇒ 加观测，不得下结论）：

- ⛔ **不实现**延迟取消。前置条件未满足，且该方案本身在待拍板清单上。
- ✅ **补观测**：`cancel()` 现在在非 `ACCESS_CANCEL` 时记
  `smsbower_cancel_refused`（带原始 `vendor_status`），异常时记
  `smsbower_cancel_failed`，两者**可区分**。契约不变（仍返回 bool、仍不抛）。
- 下一次**真实跑批**免费给出答案：日志里出现 `EARLY_CANCEL_DENIED` 就说明有窗口。

`tests/test_phone_reuse_smsbower.py` 补 6 测试 + 7 子测试钉住；已做变异验证（回退成
静默 bool 后 7 项失败）。

### 7.3 ⑨ 配置真源收敛：测量结果建议**不做**

`docs/current/configuration.md` 已把决策落档（*shards remain authoritative*），
且明确写了「保留 legacy `config.json` 支持现有安装与首次加载迁移，不自动删除本地文件」。
所以本项不是「三个互相竞争的源」，而是**一层权威（分片）+ 一条 legacy 迁移路径** ——
**这已经是报告想要的目标形态**。

测量决定性地支持保留：

| 侧 | 写 `config.json` 的测试引用 | 写分片的测试引用 |
| --- | --- | --- |
| C#（`tests/SmsWorkbench.Tests/`） | **30** | **1** |
| Python（`tests/*.py`） | — | **32** |

即**两侧测试套件写在不同的布局上**，legacy 迁移分支是 C# 那半边的**承重墙**：
删掉它等于重写约 30 处 C# 测试装置，而收益只是删掉一条现有安装仍在依赖的路径。

**建议**：维持现状。若要收敛，正确的最小改动是**把 C# 测试装置迁到分片布局**（一次性、
可验证、不碰运行时行为），而不是删运行时分支 —— 但那属于独立议题，不在本清单内。

### 7.4 ⑩ 发布前扫描：原实现有三类整类漏检

读原实现（149 行、**零测试**）时发现三类漏检，全部是静默的：

1. **值字符集不含符号** ⇒ `password: 'Zq3Xk9Mv7Rt2Lp5Wb8!'` 这种带 `!` 的明文密码整条漏掉；
   ⚠️ **该示例的原始值是对标项目里的真实明文密码，2026-09-23 已脱敏为等价人造值** ——
   **记录别人的泄漏时，不要把泄漏值本身一起搬过来**，那会让本仓也变成一个泄漏点
   （本节 §2 的「泄露清单」用「文件:行 + 类型」的写法是对的，举例时一度没守住同一条规矩）；
2. **`if val.startswith('http'): continue`** ⇒ URL 形态整类短路，硬编码的公网 IP 端点一个都报不出来；
3. 🔴 **名字恰好等于凭据词的变量，三个模式全漏**。三个正则的前缀
   `[A-Za-z_][A-Za-z0-9_]*` 都是**必填**的，`password` 没有字符可以让给前缀。实测：

   | 名字 | PAT | PAT2 | PAT3 |
   | --- | --- | --- | --- |
   | `password` / `secret` / `token` / `pwd` / `key` | False | False | False |
   | `PASSWORD` / `SECRET` / `TOKEN` / `KEY` | False | **True** | False |

   即**小写形态三个模式全漏**，只有全大写靠 PAT2 兜住 —— 而 `password = "..."`
   恰恰是最可能的硬编码写法。

**改造**：名字判定改为**按段匹配**（`credential_name`），顺手消掉 `keyboard` / `author` /
`tokenizer` / `secretary` 这一整类「前缀恰好是凭据词」的误报；两个值分支统一过
`looks_like_secret_value`；补注释与 docstring 跳过（与 `scripts/precommit_guard.py`
**有意重复**实现，由平价测试钉住两份不漂移）。

**新命中的 3 条全是误报，且两条的解法本仓早已存在**：`JBSWY3DPEHPK3PXP` 是 RFC 4226
教科书示例密钥，`precommit_guard.py` 的 `iter_scannable_lines` 早就在跳过 docstring；
`auth_state = "authenticated"` 是普通映射，靠值判定排除。全树复跑 **0 findings**。

`tests/test_scan_hardcoded_secrets.py` 补 51 测试 + 124 子测试。**已修一处实现级缺口**：
`is_public_identifier` 只认 `_` 分隔符，`OAuth-Client-Id` 会漏（已归一化 `-` → `_`）。


---

## 附：本次扫描的方法与边界

- **取证方式**：全量 clone 到本地，逐文件读取与 grep；所有结论都带 `文件:行` 或命令输出。
- **未做的验证（明确声明）**：
  - ❌ **未使用**对方泄露的任何 API Key / 账号密码发起请求（未授权使用）。
  - ❌ **未运行**对方任何代码（含 `npm install`）。
  - ⚠️ `git rev-list --count HEAD` 因 `--depth 1` 克隆只返回 1 ⇒ **提交数不是真实值**，仅能确认"工作区内容即最新提交内容"。

### 2026-09-22 执行时补齐的两项前置验证

- ✅ **已实测**本仓接码供应商是否存在「早期取消拒绝」窗口（⑦ 的前置条件）——
  结论是**不可判**：供应商答复在 `smsbower.py` 的 `cancel()` 里被吞成 bool、
  调用方又丢弃返回值，证据在任何人看到之前就被销毁。详见 §7.2。
- ✅ **已量测**本仓供应商层的新增改动成本（⑧ 的前置条件）——
  Python 侧达标（注册表 1 条 + 工厂 1 条，其余派生），C# 侧是未受守卫的漂移面。详见 §7.1。
