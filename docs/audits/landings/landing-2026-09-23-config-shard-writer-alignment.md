# 落地记录：配置分片写入器对齐（候选 1–3）

- 来源：2026-09-23 配置分片体检（`MEMORY.md` §34 的优化候选 1–3），老板拍板执行
- 日期：2026-09-23
- 范围：`sms_tool/config.py` · `sms_tool/cli.py` · `SmsWorkbench/ConfigStore.cs` · 两侧测试 · 本机 `config.json` 归档
- 回归：Python **5042 passed / 6 skipped / 1082 subtests / 0 failed**（基线 5038/6，**+4** = `test_config_sharded_atomic_write.py` 4→8 条）；C# **453 / 0 / 0**（基线 450，**+3** = `ConfigStoreTests` 3→6 条）
- 未落地（本轮明确不做）：候选 4「收敛 `SHARD_OWNERSHIP` 三份手抄」、候选 5「60 个无读取者 / 126 个未文档化叶子」、候选 6「配置目录可重定位」

---

## 结果总表

| # | 落地内容 | 主要改动 | 验证 |
|---|---|---|---|
| 1 | 归档并删除死 `config.json` | 新增 `runtime/tmp/archive_legacy_config_20260923.py`；删除项目根 `config.json` | 归档 sha256 逐字节核对通过后才删；doctor `config_source` 仍报项目根 |
| 2 | 对齐两侧原子写与空片语义 | `ConfigStore.WriteAtomic` 补 `.bak` + fsync；空片由**删文件**改为写 `{}`；`DeleteShard` 删除 | 变异 M4 命中 **3** 条红；`config_schema_check.py` 通过 |
| 3 | 写入按内容变化裁剪 | 两侧都只写内容变化的分片，并返回实际写入的文件名 | 变异 M1 命中 2 条红、M3 命中 1 条红 |
| 附 | 序列化字节对齐（**本轮新增发现**） | C# `Encoder = UnsafeRelaxedJsonEscaping` + 尾随 `\n` | 变异 M5 / M6 各命中 1 条红 |
| 附 | `default_config_dir()` 取代 `default_config_path().parent` | 新增函数；`cli.py:441` 改用它 | 变异 M2 命中 1 条红 |
| 附 | 重新发布 `dist/net10` | 复刻 `build_dotnet.ps1` 步骤；删 `obj/` 强制完整重建 | 标记串三件套（`DeleteShard` 1→0 / `ReadTextOrNull` 0→1 / 对照恒 1）· dll sha256 `f39da496…`→`59f56234…` · C# 453/0/0 |

---

## 第 1 项：归档并删除死 `config.json`

**判据**（删除前实测，可复算）：

| 项 | 实测 |
|---|---|
| 死文件叶子 | 386 |
| **独有叶子** | **0**（每个键都能在分片里找到） |
| 值不同 | 88（样本里一律是 legacy 为空/旧值、分片为真值） |
| git 状态 | `.gitignore:1` 忽略、`git log` 无历史 ⇒ 删除是**纯本机操作**，不进仓库 |

归档 `runtime/config-backups/config.json.before-archive-20260923-195004`（16330 B，
sha256 `4067fa31d40cf34d…`），**核对通过后才删**。回滚：把归档件拷回项目根。

> ⚠️ 删的只是这份数据文件。**legacy 迁移分支不能删**：`config.example.json` 是单文件，
> 首装与 CI 都靠它落地成分片。

**删后必须配套的修复**（否则引入新缺陷）：`default_config_path()` 在根 `config.json`
不存在时回落到**包内**副本，于是 `default_config_path().parent` 从项目根变成
`sms_tool/` —— 而 doctor 报的 `config_source` 正是这个值。实测：

```
修复前 cli 会传的值: …\sms_tool      -> config_source: …\sms_tool   (status: ok)
修复后 cli 传的值:   …\GPT-Register-Tool -> config_source: …\GPT-Register-Tool (status: ok)
```

🔴 **注意这条不翻转任何状态位** —— `_probe_config` 比的是 bundled **文件**路径，
传目录永远不相等，所以两侧都是 `ok`。差异只是**报告里印出的路径错了**。
（初版注释写成"会触发 bundled-fallback 误报"，实测推翻，已改正。）

**连带影响已逐一核实**：三个模块把项目根 `config.json` 当 `DEFAULT_CONFIG_PATH`
（`paypal_link/gen_link.py:1226` · `omakse_client.py:35` · `upi_link.py:303`），
但**三者都有同一个哨兵分支** `abspath(path) == abspath(DEFAULT_CONFIG_PATH)`
⇒ 转 `load_merged_config()`，**不读那个文件** ⇒ 功能无回归。

`tests/test_config_shard_access.py` 原有一条 `test_gen_link_canonical_path_exists`
断言哨兵指向的文件**必须真实存在**，前提已不成立，删除；其想守的行为
（canonical 路径必须经分片加载器）改由新增的
`test_every_module_with_a_canonical_path_routes_it_through_the_shards` 覆盖
**三个模块** —— 原先只覆盖 `gen_link`，`omakse_client` / `upi_link` 的哨兵**无人守**。

---

## 第 2 项：对齐两侧原子写与空片语义

修复前两侧差异（实测）：

| 维度 | Python | C#（修复前） | 现在 |
|---|---|---|---|
| 临时文件 | 同目录 `mkstemp` | 同目录 `path.tmp.guid` | 一致（本来就一致） |
| 替换 | `os.replace` | `File.Move(overwrite)` | 一致 |
| `.bak` | ✅ `shutil.copy2` | ❌ 无 | C# 补 `File.Copy` |
| fsync | ✅ `os.fsync` | ❌ 无 | C# 补 `Flush(flushToDisk: true)` |
| 尾随换行 | ✅ `+ "\n"` | ❌ 无 | C# 补 |
| 非 ASCII | ✅ `ensure_ascii=False` | ❌ 默认编码器转义 | C# 改 `UnsafeRelaxedJsonEscaping` |
| 空片 | 写 `{}` | **删文件** | 两侧写 `{}` |

**空片语义为什么必须改成 `{}`**：两侧都用「至少存在一个分片文件」判断「是不是分片
布局」（`config.py:166` / `AnyShardExists`），所以把三片删光会把应用**交回 legacy
单文件分支** —— 那份没人维护的旧 `config.json` 被重新读进来，复活刚删掉的键。
空对象 merge 之后不产生任何键，所以写 `{}` **同样**能防止复活，却不会翻转布局判定。
`WriteShards` 现在返回实际写入的文件名；调用方需要「三个文件都在」时应断言**文件**，
而不是这个列表 —— 一次全无变化的保存合法地返回空列表。

**序列化不一致是本轮新发现**，不在老板给的三项里，但它让第 3 项无法成立：
两侧互判对方写的文件为「已变更」，裁剪永远命中不了。取证 —— 修复前 `proxy.json`
逐字就是：

```
"country_name_zh": "\u667A\u5229",     ← C# 写的，`\u002B` 同理（`+`）
```

`UnsafeRelaxedJsonEscaping` 里的 "unsafe" 指**把 JSON 嵌进 HTML** 的场景，
配置文件不涉及。

---

## 第 3 项：写入按内容变化裁剪

`_dump_json` / `IndentedJson` 各自成为**唯一的序列化点**，裁剪按它的输出逐字节比较
（`_read_text_or_none` 用 `utf-8-sig` 读，BOM 文件不会每次都被判成已变更）。
收益：分片 mtime 重新成为「哪块配置被动过」的审计信号（§16 那次密钥写串就是靠它
分工作流的）、少两次 fsync、未变分片的旧 `.bak` 不会被无意义保存冲掉。

---

## 变异验证（每处修复注掉都必须变红）

| 变异 | 结果 | 变红用例数 |
|---|---|---|
| Python 去掉内容裁剪 | 红 ✅ | 2（`..._skips_shards_whose_content_did_not_change`、`..._returns_only_what_it_wrote`） |
| Python `default_config_dir` 退回 legacy 语义 | 红 ✅ | 1 |
| C# 去掉内容裁剪 | 红 ✅ | 1 |
| C# 恢复「空片删文件」 | 红 ✅ | **3** |
| C# 去掉 `Encoder` 对齐 | 红 ✅ | 1 |
| C# 去掉尾随换行 | 红 ✅ | 1 |
| 基线（无变异） | 绿 ✅ | 0 |

---

## 坑清单

1. 🔴 **引入裁剪会让「写两次」的测试变成同义反复。**
   `test_write_shards_keeps_old_file_when_replace_fails` 原本第二次写**相同**内容 ——
   加上裁剪后 `os.replace` 根本不会被调用，被 patch 的失败路径永远不触发，
   测试**永远通过**却什么都没证明。已改成第二次写**不同**内容。
   任何「写两次 / 重放 / 幂等」的测试在引入裁剪后都要重查这一点。
2. 🔴 **不能靠「测试绿」判断修复有效**：上面 6 处修复全部有对应的绿测试，
   但只有注掉修复点变红才证明测试真的在守它。M4 命中 3 条、M1 命中 2 条，
   是「一处修复有几条守卫」的量化答案。
3. 🔴 **删文件类改动必须配套查「谁按路径找它」**。删 `config.json` 表面上只是
   清掉一个死文件，实际连带 `default_config_path().parent` 的语义漂移
   （doctor 报错目录）与一条测试前提失效。三个 `DEFAULT_CONFIG_PATH` 模块
   逐个核实过哨兵分支才敢删。
4. 🔴 **不要凭直觉删 `SHARD_OWNERSHIP` 的守卫**：它已有三方守卫
   （`config.py` / `ConfigStore.cs` / `config_schema.json`，由
   `scripts/config_schema_check.py` 比对，CI 第 71 行在跑），实测 23/23 键一致。
5. ✅ **`dist/net10` 已重新发布**（见下节），本轮 C# 修复已在分发产物里。
   ⚠️ 但 `dist/installer/package/dist/net10/SmsWorkbench.dll` 仍是 **09-13** 的
   （782336 B，比 `dist/net10` 陈旧 10 天），且 `dist/installer/package/` 下还有一份
   **Sep 5 的陈旧源码副本** —— 按源码 grep 做审计时会被它误导。这两处要等重建安装包才会刷新。

---

## 重新发布 `dist/net10`（2026-09-23 22:08）

**没走正本入口** `SmsWorkbench/build_dotnet.ps1` —— 它在任何 publish 之前就误抛：
`if ($LASTEXITCODE -ne 0) { throw }` 里 `$LASTEXITCODE` 为 `$null`（PowerShell 工具会话
不回填原生命令退出码），`$null -ne 0` 为真。按 `sandboxed-dotnet-wpf-build` 技能
**复刻入口脚本的步骤**，从 Bash 走 `scripts/inject_dotnet_env.py`：

| 步骤 | 做法 | 结果 |
|---|---|---|
| ① 删退役产物（WebView2 一族 8 个） | 逐个 `Test-Path` | 均不存在，无需删 |
| ② 强制完整重建 | 删 `SmsWorkbench/obj`(4.5 M) + `SmsWorkbench.Contracts/obj`(337 K) | `--no-incremental` 不存在、清 `bin/` 无效，**只有删 `obj/` 有效** |
| ③ publish | `inject_dotnet_env.py ".dotnet/dotnet.exe" publish SmsWorkbench/SmsWorkbench.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=false -o dist/net10` | **exit=0 · 0 error · 106 warning**（全是既有 CA2016/CA1001）· 20 s |
| ④ 跑测试 | `dotnet test GPTRegisterTool.slnx -c Release` | **453 / 0 / 0**（与发布前一致） |
| ⑤ 清 `bin`（**必须最后**） | `clean_dotnet_workspaces.ps1` | `SmsWorkbench/bin` 已消失 |

### 产物核验（三条独立判据）

**① 标记串成组扫描**（单标记的"搜不到"无法区分"修复没进产物"与"扫描器失效"，
故用「删除项 + 新增项 + 对照项」三件套）：

| 标记 | 角色 | 改前 `dist/net10` | 改后 `dist/net10` |
|---|---|---|---|
| `DeleteShard` | 本次**删除**的方法 | 1 | **0** |
| `ReadTextOrNull` | 本次**新增**的方法 | 0 | **1** |
| `AnyShardExists` | **没动过**的对照 | 1 | 1 |

对照项始终为 1 ⇒ 扫描器在这份文件、这个编码上确实能命中 ⇒ 另外两项的变化可信。

**② 主程序集哈希**（🔴 **只看 `.dll`**）：

```
dist/net10/SmsWorkbench.dll   f39da496f81e71fb… (795648 B, 17:24)
                          →   59f5623426f93aeb… (796160 B, 22:08)
```

**③ `.exe` 哈希逐字未变**：`03ee9b231819214c…`（268800 B）—— 这正是 apphost 启动桩的陷阱：
framework-dependent 的 `.exe` 只依赖程序集名/版本，**真实重建后字节可以完全相同**。
拿它判"没重建"就是误判。（反向也不成立：同一工程两次 publish 的 `.exe` 也可能变。）

**残留扫描**：`rglob` 遍历 `dist/net10` 全部 `.dll/.exe/.pdb/.json` ⇒ `DeleteShard` **零命中**、
`ReadTextOrNull` 仅命中 `SmsWorkbench.dll` ⇒ 无旧符号残留。
⚠️ 计数口径：`ls dist/net10 | wc -l` = **43**（顶层条目，与改前相同）、`rglob` = **51**（递归文件）
—— 两个数字不同不代表产物变了。

**清理未误伤发布目录**：清 `bin` 后复核 `dist/net10/SmsWorkbench.dll` 的 sha256 仍为
`59f56234…`、标记串三项不变；`obj/` 仍在（4.3 M）属正常，`clean_dotnet_workspaces.ps1`
按设计不清 `obj/`。
6. ⚠️ **`.bak` 的 ignore 状态已核实**：`.gitignore` 的 `*.bak` 与家族规则
   `proxy.json*` / `runtime.json*` / `payment.json*` 覆盖 ⇒ C# 新增的 `.bak`
   不会在仓库根制造未跟踪文件。

---

## 回滚路径

| 改动 | 回滚 |
|---|---|
| 删除 `config.json` | `copy runtime/config-backups/config.json.before-archive-20260923-195004 config.json` |
| 三处代码改动 | `git checkout -- sms_tool/config.py sms_tool/cli.py SmsWorkbench/ConfigStore.cs` + 还原两个测试文件 |
