# v2026.09.23

本版本收口 2026-09-23 同日的三项工作：桌面端「一键接码」按协议族分派并让 NeXSMS
接通在线目录（余额 / 国家 / 档位 / 库存全部落地）、接码供应商密钥跨 section 写串
事故的修复与离线守卫、配置分片写入器两侧对齐（含死 `config.json` 归档）。

---

## 一键接码：NeXSMS 接通在线目录

### 现象

切到 NeXSMS 点「一键接码」，余额恒为 `--`，国家与档位各只有 1 项 —— 就是配置里
`phone_reuse.nexsms.country` / `.target_price` 那一对。另外三家（smsbower / herosms /
grizzly）正常显示多国多档。

### 根因

`MainWindow.SmsProvider.cs` 对 `CatalogIsSmsActivate == false` 的供应商走**完全离线**
的 `else` 分支：不读在线目录，只把配置里那一对包成一项。NeXSMS 属 `nexsms_json`
协议族 ⇒ 必然命中这条分支。

`CatalogIsSmsActivate` 的语义是「该用哪个读取器」，不是「能不能读目录」。注释里没写清
这一点，是这条分支被写出来的直接原因（已连同本次修复改写）。

### 修法

`SmsProviderCatalogClient` 新增 NeXSMS 读取器。三个端点全部实测确定，不是照文档猜的：

| 端点 | 返回 |
| --- | --- |
| `GET /api/balance` | `{"code":0,…,"data":{"balance":"0.2000"}}` —— `balance` 是**字符串** |
| `GET /api/countries` | 195 行 `{id, name}`，`name` 为**中文**，**无英文名字段** |
| `GET /api/getCountryByService?serviceCode=dr` | 183 行，含 `countryId` / `countryName`(英文) / `priceMap` / `phoneCode` |

信封是 `{code, message, data}`，`code` 用**数值**比较（`"0"` 也算成功）。`priceMap` 形如
`{"0.1207": 56174, …}` —— 键为价格字符串、值为库存，本地按数值升序排序，不信任载荷顺序。

对话框的分派由二分改为**三元**，两个协议族各走自己的读取器：

```csharp
online = provider.CatalogIsSmsActivate
    ? await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(httpClient, apiKey, endpoint)
    : await SmsProviderCatalogClient.LoadNexsmsCatalogAsync(httpClient, apiKey, endpoint, service);
```

⚠️ 路径要追加到 endpoint 的 **path 部分**而不是整串 —— 否则 endpoint 带查询串时
（如 `https://host?tenant=1`）`/api/...` 会被折进查询串，请求打不到路由。

### 实测对照（真实 API，端到端探针）

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 余额 | `--` | **0.2000** |
| 国家数 | 1 | **183** |
| 档位数 | 1 | **923** |
| 库存合计 | 未查询 | **125,257,831** |
| 中文名覆盖 | 无 | **183/183** |

配置国家 `6`（印尼）命中、`0.1207` 档存在、全档升序、零档未知库存。

### 顺带删除两处弹窗说明文案

两处都不是错误信息，而是「解释为什么只能这样」的旁白，对使用者是噪声：

1. 对话框里的整段说明（「NeXSMS 不使用 sms-activate 协议，桌面端不读取在线目录…」）
   —— 连同承载它的 `notice` 变量与整个 TextBlock 渲染块一起删除。
2. 档位行的「（取自配置，未查询库存）」后缀 —— 它读起来像在给**价格**加限定，而
   它从来不是。

失败原因改为分两处、各自面向正确的读者：**无路可退**（在线目录失败且配置里也没有可用
国家与档位）→ 弹窗插值；**有路可退**（回退到配置值）→ 日志。

> 保留 `SmsProviderPriceTier.DisplayName` 的「库存未查询」后缀 —— 那是下拉项文案，
> 与上面第 2 条不是同一处。

---

## 接码供应商密钥跨 section 写串

### 现象

三家同时报错，看起来像「厂商都坏了」：HeroSMS `401 (Unauthorized)`、Grizzly SMS
`NOKEY`、NeXSMS `403 (Forbidden)`。三条弹窗文案都是「无法读取 OpenAI 号码地区和价格
档位：…」，**既不说哪一家、也不说哪个配置键**。实际 `smsbower` 一直是好的。

### 两个独立根因

| # | 根因 |
| --- | --- |
| 1 | 「设置 → 接码供应商」的**下拉框与输入框是两个独立控件**：`SettingsService.Load()` 只在开窗时解析一次 `phone_reuse.{provider}.*`，改选供应商后没有任何东西重新解析 ⇒ 输入框里仍是**开窗时那家**的 key，操作者选另一家再保存，`Save()` 就把它写进了**新选中**的 section |
| 2 | 装到机器上的桌面端二进制是 **11:25** 构建的，早于两个修复提交 ⇒ NeXSMS 还在走 sms-activate 协议族，直接 403 |

**关键判据**：把「最后一次已知良好备份 + geo 脚本复现 + 逐叶子比对」做出来后，差异
**100% 落在设置界面分类拥有的字段上**，且每个值都等于 smsbower 的值（三家的
`api_key` 逐字相同、`endpoint` 被写成 `""`、`sms_timeout` 全变 60）。

> ⚠️ `Save()` 里的 `ResolveProviderPath` 修的是**写入目标**，不是**显示值**。只修前者
> 反而更糟：密钥从「写到自己家（无害）」变成「写到别人家、还顶着别人家的名字
> （静默错配）」。

### 修法与守卫

- **数据**：按 provider 切块做块内字节级精确替换（四家 key 现值逐字相同 ⇒ 全局替换
  改不出三家不同的值），6 道校验全过才落盘。
- **代码**：`SettingsService.ReloadProviderScopedFields(fields, provider)` 按**显式**
  provider 重解析所有带 `{provider}` 的字段；`SettingsViewModel.WatchProviderSelector()`
  订阅 `phone_provider` 字段的 `PropertyChanged`，下拉一变就重载 ⇒ 输入框显示的
  **永远是这次保存真正会写进去的值**。订阅挂在**字段**上而非窗口代码：字段由
  `SettingsCatalog` 声明、通用渲染，没有 per-field 控件可挂，且这样不必构造窗口即可测试。
- **守卫（离线）**：`phone_reuse.provider_key_collisions(cfg)` —— 两家以上**解析后**的
  api_key 逐字相同即告警并点名 section；**不打印密钥、不发请求**。`--doctor` 已接入，
  且不掩盖「选中那家没有 key」（挡住跑批的是它，撞车只是更意外）。

---

## 配置分片写入器对齐（候选 1–3）

### 1 归档并删除死 `config.json`

判据（删除前实测，可复算）：386 个叶子 / **独有叶子 0** / 88 个值不同（样本里一律是
legacy 为空或旧值、分片为真值）。归档 sha256 逐字节核对通过后才删。git 侧无历史 ⇒
删除是**纯本机操作**。

**配套修复**（否则引入新缺陷）：`default_config_path()` 在根 `config.json` 不存在时回落
到**包内**副本，于是 `default_config_path().parent` 从项目根变成 `sms_tool/`，而 doctor
报的 `config_source` 正是这个值 ⇒ 新增 `default_config_dir()` 并改用它。
⚠️ 这条**不翻转任何状态位**（两侧都是 `ok`），差异只是报告里印出的路径错了。

三个模块把项目根 `config.json` 当 `DEFAULT_CONFIG_PATH`，但**三者都有同一个哨兵分支**
`abspath(path) == abspath(DEFAULT_CONFIG_PATH)` ⇒ 转 `load_merged_config()`，**不读那个
文件** ⇒ 功能无回归。

### 2 两侧原子写与空片语义对齐

| 维度 | Python | C#（修复前） | 现在 |
| --- | --- | --- | --- |
| 临时文件 / 替换 | 同目录 `mkstemp` / `os.replace` | 同目录 `.tmp.guid` / `File.Move(overwrite)` | 一致 |
| `.bak` / fsync / 尾随换行 | ✅ / ✅ / ✅ | ❌ / ❌ / ❌ | C# 补齐 |
| 非 ASCII | `ensure_ascii=False` | 默认编码器转义 | C# 改 `UnsafeRelaxedJsonEscaping` |
| 空片 | 写 `{}` | **删文件** | 两侧写 `{}` |

**空片语义为什么必须改成 `{}`**：两侧都用「至少存在一个分片文件」判断「是不是分片
布局」（`config.py` / `AnyShardExists`），把三片删光会把应用**交回 legacy 单文件分支**
—— 那份没人维护的旧 `config.json` 被重新读进来，复活刚删掉的键。空对象 merge 之后不
产生任何键，所以写 `{}` **同样**能防止复活，却不会翻转布局判定。

### 3 写入按内容变化裁剪

两侧各自成为**唯一的序列化点**，裁剪按它的输出逐字节比较。收益：分片 mtime 重新成为
「哪块配置被动过」的审计信号、少两次 fsync、未变分片的旧 `.bak` 不会被无意义冲掉。

### 附：序列化字节对齐（本轮新发现）

不在老板给的三项里，但它让第 3 项无法成立 —— 两侧互判对方写的文件为「已变更」，
裁剪永远命中不了。取证是修复前 `proxy.json` 里逐字就是：

```
"country_name_zh": "\u667A\u5229"
```

C# 默认 `JavaScriptEncoder` 把 `+` / CJK 转义成 `\u002B` / `\u667A\u5229`，与 Python 的
`ensure_ascii=False` 不一致。`UnsafeRelaxedJsonEscaping` 里的 "unsafe" 指**把 JSON 嵌进
HTML** 的场景，配置文件不涉及。

> ⚠️ 引入裁剪后，任何「写两次 / 重放 / 幂等」的测试都会变**同义反复**（`os.replace` 不再
> 被调用）。实测 `test_write_shards_keeps_old_file_when_replace_fails` 原本第二次写**相同**
> 内容，加上裁剪后被 patch 的失败路径永远不触发 —— 测试永远通过却什么都没证明。
> 已改成第二次写**不同**内容。同类测试在引入裁剪后都要重查这一点。

---

## 验证

- **Python**：`pytest tests/` —— **5042 passed / 6 skipped / 0 failed**（1084 subtests）。
  新增：`test_config_sharded_atomic_write.py` 4→8 条、`test_doctor.py` 撞车判据、
  `test_settings_catalog_provider_parity.py` 4 条守卫改锚点到三元分派。
  ⚠️ 首轮全量出现过 1 条时间敏感用例偶发红
  （`test_environment_ledger.py::test_a_lease_past_its_ttl_frees_the_exit`，用
  `ttl_seconds=1` + `time.sleep(1.2)` 卡边界），单独复跑 **3/3 通过**，全量复跑通过。
- **.NET**：`dotnet test GPTRegisterTool.slnx -c Release` —— **467 passed / 0 failed /
  0 skipped**（基线 450）。新增：`SmsProviderNexsmsCatalogTests` 12 条、
  `ConfigStoreTests` 3 条、`SettingsServiceTests` 2 条。
- **构建**：删 `SmsWorkbench/obj` + `SmsWorkbench.Contracts/obj` 后完整重建
  `dist/net10`，**0 error**（106 条既有 CA2016/CA1001 warning）。
  主程序集 `SmsWorkbench.dll` 796,160 B → 801,280 B。
  ⚠️ **不要把 `.dll` 的 sha256 当发布指纹**：.NET SDK 会把构建时的 HEAD 提交编进
  `ProductVersion`（形如 `0.0.0+<sha>`），此后任何一次提交都会让它变（上一版
  `v2026.09.14` 的 dll 内嵌 `3ac5ab4`，而 tag 指向 `1b1c554` —— 纯文档提交走增量
  构建，dll 根本没被重新产出）。判「真的重建了」只看 `.dll` 的**尺寸**与标记串。
  `.exe` 是 apphost 启动桩，字节可以逐字不变 ⇒ **只认 `.dll`**。
- **产物标记串（18 条，成组，只用纯字面量）**：
  ```
  本次删除：4 条              → 全部 absent
  本次新增：9 条（含 3 个 /api/ 路径）→ 全部 present
  没动过  ：5 条对照          → 全部 present
  FAILURES: 0
  ```
  🔴 两条判据不写死，扫描就会给出**假阴性**（本次实测踩满）：
  - **编码按堆分**：字符串字面量在 `#US` 堆里是 **UTF-16LE** —— 连纯 ASCII 的
    `/api/balance` 也是（用 `.encode()` 扫得 **0**）。方法 / 类型 / 字段名在
    `#Strings` 堆里才是 **UTF-8**（`ReloadProviderScopedFields` 实测 utf16=0）。
    本次 9 条新增里 **8 条只能靠 UTF-16LE 命中**。
  - **内插字符串不能当标记**：`$"价格 {tier.Price} / 个"` 编译后**拆成字面量片段**
    （实测产物里 `价格 ` 命中 2 次、` / 个` 命中 3 次，而整串与复合格式串
    `价格 {0} / 个` **都是 0**）⇒ 凡含 `{}` 的候选一律不选；想验某条文案被删，
    只能验**长且不含插值的片段**（短片段如 ` / 个` 会撞上别处，不成凭据）。
  对照项（`getPricesV3` / `getCountries` / `ACCESS_BALANCE:` / `AnyShardExists` /
  `库存未查询`）全部命中，才是「扫描器在这份文件、这个编码上有效」的凭据。
  复现：`python runtime/tmp/_scan_binary_markers_v2.py dist/net10/SmsWorkbench.dll`
- **守卫**：architecture_scan / docs_consistency_scan / config_schema_check /
  ipc_schema_check / ruff / 硬编码秘密扫描（747 文件 / 0 findings）/ 发布载荷扫描全部通过。
- **变异**：本轮三组各 1 / 6 / 12 处，**全部被捕获**（注掉修复点即变红）。

---

## 回滚路径

| 改动 | 回滚 |
| --- | --- |
| NeXSMS 目录接通 | `git checkout -- SmsWorkbench/SmsProviderCatalogClient.cs SmsWorkbench/MainWindow.SmsProvider.cs SmsWorkbench/SmsProviderCatalog.cs` + 删新增测试 |
| 密钥写串修复 | `git checkout -- SmsWorkbench/SettingsService.cs SmsWorkbench/SettingsViewModel.cs sms_tool/phone_reuse.py sms_tool/doctor.py` + 还原四个测试文件 |
| 配置分片对齐 | `git checkout -- sms_tool/config.py sms_tool/cli.py SmsWorkbench/ConfigStore.cs` + 还原三个测试文件 |
| 删除 `config.json` | `copy runtime/config-backups/config.json.before-archive-20260923-195004 config.json` |
| 桌面端产物 | 重跑 `publish`（先删 `obj/`）；上一版指纹 `59f56234…`(796160 B) |

---

## 附：本次一并清理的本地产物

`runtime/tmp` 下 84 个条目（含 15 个备份目录、`got.txt` 等含真实凭据的一次性 dump，
共 9.8 MB）退役到 `runtime/_retired_20260924/`；56 个 flaky 循环日志与空目录删除；
`dist/` 三个陈旧构建日志删除。**被跟踪文档/源码引用的 49 个复现脚本一律保留**
（如 `p0_compare.py` / `p0_proxy_diff.py` 被支付模块源码点名，删了会让公开仓库的引用悬空）。
