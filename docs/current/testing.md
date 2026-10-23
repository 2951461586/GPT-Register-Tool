# 测试执行：分片、并行与顺序敏感边界

`python -m pytest -q` 是唯一入口；本文只说明**哪些域可以并行**、为什么有一个
必须单独跑的顺序敏感分片，以及实测数字。门禁清单本身在
[`dotnet-gates.md`](dotnet-gates.md) 与 [`typing.md`](typing.md)。

## 为什么引入并行

串行全量在 2026-10-20 的实测是 **620 s**（6627 passed / 10 skipped），其中
`tests/governance` 单独占 **304 s**（它会真的跑 pyright、真的 rebuild .NET、
真的扫整棵树的门禁），`tests/payment` 114 s，`tests/registration` 70 s。

启用 `pytest-xdist` 后（`-n 4 --dist loadfile`）：

| 命令 | 串行 | 并行 | 结果 |
| --- | --- | --- | --- |
| 全量 `pytest -q` | 620.7 s | **220.7 s** | 两者都是 6627 passed / 10 skipped / 1191 subtests |
| `tests/payment tests/config tests/accounts tests/mailbox tests/proxy` | 195.3 s | **61.0 s** | 3924 passed / 2 skipped，两次一致 |
| `tests/governance` | 304 s | **144.2 s** | 555 passed / 5 skipped |
| `tests/registration tests/workbench` | 88 s | 23.3 s（不采用，见下） | 2148 passed |

结论：并行把全量压到 **约 1/3**，且结果集完全相同。

## 为什么用 `--dist loadfile`

仓库里大量测试用 `patch.object` 改**模块级状态**，并把模块当单例用
（`proxy_entry`、`shared_*_pool`、`mailbox` hub、`store` 的进程级路径 provider）。
`loadfile` 保证**同一个文件的测试落在同一个 worker 上**，文件内的顺序与
monkeypatch 生命周期不变，跨文件之间本来就不该有顺序契约。默认的 `load`
按单测分发，会把同一文件的测试拆到不同 worker，正是最容易踩这类状态的切法。

## 顺序敏感分片：为什么仍然是独立的串行进程

`ci.yml` 里 `pytest -q tests/registration tests/workbench` 这一步**保持串行、且
在并行全量之后单独起进程**。这不是保守，而是它抓到过真缺陷（2026-10-18）：
`cli._heavy_deps_loaded` 是一次性闩锁，某个测试在 `_load_mailbox_pool` 被 patch
时把它置真，`patch.object` 把符号还原成 `None`、闩锁却留了下来，workbench 的
一键命令随后调用 `None(...)`。全量套件之所以绿，是因为更早的测试在 patch 之外
解析过同一批符号。

也就是说，这一步的价值来自**「没有那些早期解析的干净进程」**。实测 `-n 4` 下它
也能通过（23 s），但那只是说明当前没有第二个同类缺陷——把它并入并行分片等于
删掉这个守卫，所以它继续单独跑。

## 命令

```bash
# 本地全量（并行，3-4 分钟）
python -m pytest -q -n 4 --dist loadfile

# 顺序依赖守卫（必须单独跑、保持串行）
python -m pytest -q tests/registration tests/workbench

# 只跑一个域
python -m pytest -q tests/payment
```

`-n auto` 会按本机核数起 worker（开发机 12 核会起 12 个），每个 worker 都要
import 整个包并各自跑真实门禁；CI 与文档统一用 **`-n 4`**，让耗时与内存可预期。

## 每个 worker 一个配置目录：为什么必须

`tests/conftest.py` 在**任何 `sms_tool` import 之前**把 `GPT_REGISTER_CONFIG_DIR` 指到一个
从 `config.example.json` 播种的临时目录，避免套件读到（并在首装迁移里**移动**）操作者的
真实凭据文件。

并行时这里有个不显眼的坑：**controller 进程也会 import `tests/conftest.py`**，而 xdist 的
worker 是**继承其环境变量**的子进程 —— 朴素写法等于把同一个目录发给全部 4 个 worker。
于是每个 worker 同时跑「legacy `config.json` → 三片」的首装迁移，而
[`configuration.md`](configuration.md) 的优先级是**「任一分片存在 ⇒ 分片即权威，不回头
用 legacy 补齐」**，所以「只发布了 `proxy.json`」的瞬间被读到，就是一份没有 `chatgpt`
段的配置：

```text
ERROR collecting tests/mailbox/test_email_otp_filtering.py
sms_tool.config.schema.ConfigError: chatgpt must be an object
```

这正是 2026-10-23 的 CI 失败（两次运行各挂一个 worker），因为该文件在 import
`sms_tool.registration` 时就读配置（`sentinel_tokens` 的模块级 `runtime_file(CFG, ...)`），
窗口落在**收集阶段**，任何 fixture 都来不及介入。

因此 conftest 给 controller 建目录时打 `GPT_REGISTER_TEST_CONFIG_DIR_OWNER=1`，worker
看到这个标记（且 `PYTEST_XDIST_WORKER` 已设）就**再建一个自己的**目录。操作者显式设置的
`GPT_REGISTER_CONFIG_DIR` 没有标记，永远不被替换。守卫：
`tests/governance/test_worker_config_isolation.py`（用真子进程重放这次交接）。

写入侧同时把 `_write_shards` 改成**两阶段发布**（先 staged 全部变更分片，再连续 rename），
把「读者看到半个分片集」的窗口从两次 `fsync` 压到三次 rename。彻底消除需要跨进程锁，
所以**测试侧隔离才是主修复** —— 不要因为写入侧变窄了就把这条隔离去掉。

## 依赖与门禁

`pytest-xdist` 在 `requirements-dev.txt`（测试工具，不是运行时依赖），装法与
其它开发工具一致：`python -m pip install -r requirements-dev.txt -c constraints.txt`。
`pytest.ini` 不设全局 `addopts`：并行是命令行显式开启的，默认仍是串行，
所以任何脚本或钩子里的裸 `pytest` 行为不变。
