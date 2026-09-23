# 接码供应商「加载失败」三连 —— 密钥跨 section 写串

**日期**：2026-09-23 17:15（报障）→ 17:30（修复完成）
**范围**：一键接码模块（桌面端 `SmsWorkbench` + Python `sms_tool`）+ `proxy.json` / `config.example.json`
**结论**：三条报障是**两个独立原因**，都与厂商无关。四家密钥里三家被写成了 smsbower 的密钥；
装到机器上的桌面端二进制是修复前的旧构建。

---

## 1. 报障与归因

| # | 报障原文 | 根因 | 状态 |
|---|---|---|---|
| 1 | HeroSMS：`…401 (Unauthorized).` | `phone_reuse.herosms.api_key` 里是 **smsbower 的 key** | ✅ 已修 |
| 2 | Grizzly SMS：`…NOKEY` | 同上（grizzly 对错 key 回 `HTTP 200` + 文本 `NO_KEY`） | ✅ 已修 |
| 3 | NexSMS：`…403 (Forbidden).` | **桌面端二进制是 11:25 构建的**，早于 `7298f77`(14:41) / `2677083`(15:23) | ✅ 已修 |

三条弹窗的文案都是「无法读取 OpenAI 号码地区和价格档位：…」，**既不说哪一家、也不说哪个配置键**，
所以第一眼像是「这三家厂商都坏了」。实际 `smsbower` 一直是好的。

---

## 2. 四家供应商配置现状（修复后）

### 2.1 前端（桌面端）

| 供应商 | 显示名 | 协议族 | 一键接码是否读在线目录 | 默认端点 |
|---|---|---|---|---|
| `smsbower` | SMSBower | `sms_activate_handler` | 是 | `https://smsbower.page/stubs/handler_api.php` |
| `herosms` | HeroSMS | `sms_activate_handler` | 是（V3 404 ⇒ 回退 `getPrices`） | `https://hero-sms.com/stubs/handler_api.php` |
| `grizzly` | Grizzly SMS | `sms_activate_handler` | 是 | `https://api.grizzlysms.com/stubs/handler_api.php` |
| `nexsms` | NexSMS | `nexsms_json` | **否**（不走 sms-activate，直接取配置） | `https://api.nexsms.net` |

前端与 Python 注册表的 key / label / 端点 / 环境变量名 / 协议族由
`tests/test_settings_catalog_provider_parity.py` 双向钉住。

### 2.2 后端（`proxy.json` 的 `phone_reuse.*`）

| 供应商 | api_key | 国家 | 三价 | endpoint | sms_timeout | 余额（实测） |
|---|---|---|---|---|---|---|
| `smsbower` | 32 位十六进制 | `151` 智利 | 0.07 | （未配，用默认） | 60 | **1.109** |
| `herosms` | 32 位十六进制 | `4` | 0.03 | ✅ 已配 | 120 | **7.6232** |
| `grizzly` | 32 位十六进制 | `3` | 0.013 | ✅ 已配 | 120 | **8.8000** |
| `nexsms` | 16 字符 | `6` | 0.1207 | ✅ 已配 | 120 | **0.2000** |

`phone_reuse.source` = **`nexsms`**（见 §6 待拍板）。

`--doctor`：`phone_providers: selected=nexsms (origin=config, endpoint=https://api.nexsms.net);
smsbower=config, herosms=config, grizzly=config, nexsms=config`

---

## 3. 取证链

1. **四家的 `api_key` 逐字相同**（同一个 32 位值，四家全部指向它），与「herosms/grizzly 32 位、
   nexsms 16 位」矛盾。本文与排障文档**一律不记录密钥本身或其片段**——只记长度与「是否撞车」。
2. 遍历 `runtime/proxy-backups/proxy.json*` 定位**最后一次已知良好**状态：
   `…before-herosms-grizzly-geo-20260923-150155`（4043 B，`source=smsbower`，四家密钥各不相同）。
3. **差异归因**：把该备份按 `apply_herosms_grizzly_geo.py` 的 country/价格改写复现，再与当前文件
   **逐叶子**比对 ⇒ 差异 **100% 落在「设置 → 接码供应商」分类拥有的字段上**，且每个值都等于 smsbower 的值：

   | 叶子 | 良好状态 | 被覆盖后 | 解释 |
   |---|---|---|---|
   | 三家的 `.api_key` | 各自的值 | 全部 = smsbower 的 key | 输入框没刷新 |
   | 三家的 `.endpoint` | 完整 URL | `""` | smsbower 没配 endpoint |
   | 三家的 `.sms_timeout` | `120` | `60` | smsbower 的值 |
   | `phone_reuse.source` | `smsbower` | `nexsms` | 最后一次下拉选择 |

4. **密钥真伪（零成本余额查询）** —— 恢复的 key 四家全 `HTTP 200`；当前错 key 分别回
   `401 {"title":"BAD_KEY"}` / `200 NO_KEY` / `401 {"message":"API密钥无效"}`，**与报障文案逐字对上**。
5. **403 的归属**：`https://api.nexsms.net?api_key=…&action=getCountries|getPricesV3|getBalance`
   实测全 **403**（`/stubs/handler_api.php` 变体 404）；且 `dist/net10/SmsWorkbench.dll`（11:25）
   里 `2677083` / `7298f77` 的标记串 **utf16 扫描一条都没有** ⇒ 硬证据：装的是修复前的二进制。

---

## 4. 代码根因（本次真正修的东西）

**「设置 → 接码供应商」的下拉框与输入框是两个独立控件，切换下拉框不刷新 provider 作用域字段。**

`SettingsService.Load()` 只在打开弹窗时解析一次 `phone_reuse.{provider}.*`；改选供应商之后
没有任何东西重新解析。于是输入框里仍是**开窗时那家**的 key，操作者选另一家再保存，
`Save()` 就把它写进了**新选中的** section。

> 🔴 `Save()` 里的 `ResolveProviderPath` 修的是**写入目标**，不是**显示值**。只修前者反而更糟：
> 密钥从「写到自己家（无害）」变成「写到别人家、还顶着别人家的名字（静默错配）」。
> 本次实测的 `proxy.json` 正是后者。

---

## 5. 修复与守卫

**A. 数据**（`runtime/tmp/restore_provider_keys_20260923.py`，字节级、先全校验再落盘）
按 provider 切块做块内精确替换（四家 key 现值逐字相同 ⇒ 全局替换改不出三家不同的值）；
备份 `runtime/proxy-backups/proxy.json.before-restore-provider-keys-20260923-171950`；
`proxy.json` 3949 → **4048 B**；变更叶子 9 个（三家 × `api_key` / `endpoint` / `sms_timeout`）。

**B. 代码**
- `SettingsService.ReloadProviderScopedFields(fields, provider)`（新，进 `ISettingsService`）——
  按**显式** provider 重解析所有带 `{provider}` 的字段。
- `SettingsViewModel.WatchProviderSelector()` 订阅 `phone_provider` 字段的 `PropertyChanged`，
  下拉一变就重载 ⇒ 输入框显示的**永远是这次保存真正会写进去的值**。
  订阅挂在**字段**上而非 `SettingsWindow.xaml.cs`：字段由 `SettingsCatalog` 声明、通用渲染，
  没有 per-field 控件可挂，且这样不必构造窗口即可测试。

**C. 守卫（离线）** `phone_reuse.provider_key_collisions(cfg)`：两家以上**解析后**的 api_key
逐字相同即告警并点名 section；不打印密钥、不发请求。`--doctor` 的 `phone_providers` 已接入，
**且不掩盖「选中那家没有 key」**（挡住跑批的是它，撞车只是更意外）。
回放验证：对修复前的备份 → `WARN`（点名四家）；对当前配置 → `OK`。

**D. 重建**：删 `SmsWorkbench/obj` + `bin` 后 `publish -c Release -r win-x64 -o dist/net10`
⇒ 795648 B / `sha256 f39da496…` / 17:24；四个旧修复标记 + 两个新成员名全部命中。

---

## 6. 验证计数

| 套件 | 结果 | 基线 |
|---|---|---|
| Python 全量 | **5038 passed / 6 skipped / 0 failed**（1080 subtests） | 5024（`2677083` 之前，其余增量来自该提交自身） |
| C# 全量 | **450 / 0 / 0** | 433 + `2677083` 的 15 条 + 本次 2 条 |

**变异验证**：注掉 `WatchProviderSelector()` ⇒ 新测试以
`Expected: "hero-key" Actual: "bower-key"` 变红 —— 事故本身的签名，证明它不是在测同义反复。

**改动面**：9 个文件，+437 / −9。

---

## 7. 本次未动、需拍板

1. **`phone_reuse.source` 仍是 `nexsms`**（很可能是下拉框遍历的残留，非刻意选择）。nexsms 余额
   **0.2000**，按 `target_price=0.1207` 只够 **1 个号** ⇒ 现在点「一键接码」几乎必然租不到。
   切回 `smsbower` 只需改这一个叶子。
2. **`config.example.json` 三家仍是 `country=38` + `max_price=0.06` + `target_price=0.054`**：
   实测最低价 herosms `4@0.0300` / grizzly `3@0.0130` / nexsms `6@0.1207` ⇒ 模板**结构性买不到号**
   （nexsms 的 0.06 上限连最低档都够不着）。真实 `proxy.json` 已按实测值改好，只有模板没跟上。
   未改理由：CI 的 `config.json` 是 example 的副本，改它会影响那条 CI 专属 skip 行为 —— 属独立变更。
3. `herosms` / `grizzly` / `nexsms` 仍是 `min == max == target` 三价钉单档：把会漂移的快照写进配置，
   档位一动就静默失效，症状是 `NO_NUMBERS`（看不出是配置问题）。
