# ADR-0013: 运行形态与分发：内置 CPython、只读常驻通道、可选 worker

- Status: Accepted
- Date: 2026-10-19

## 决定

1. **安装包内置固定 CPython。** `scripts/runtime_manifest.json` 固定
   `astral-sh/python-build-standalone` 的一个 Windows 归档（URL + SHA-256），
   `scripts/stage_python_runtime.ps1` 校验后解包到 `runtime/python` 并把依赖装进去。
   `PythonPathResolver` 的优先级是**显式 `runtime.python_path` > 内置解释器 > PATH**。
2. **只读常驻通道保留，命令 RPC 暂不实现。** `desktop_serve.py` 继续只服务读操作；
   注册/支付等长任务维持一次性进程。
3. **Sentinel 常驻 worker 默认关闭。** `SMS_SENTINEL_WORKER=1` 才启用；它只换传输，
   runner 判定与 token 校验完全复用，传输失败才回退一次性 node。
4. **浏览器池增加 RSS 回收阀。** `registration.browser_process_pool.max_total_rss_mb`
   默认 0（关闭）；开启后超限只回收一个空闲槽位，采样失败降级为“不测量”。
5. **性能预算用宽松上限 + 结构化断言。** 入口 import/RSS/help 有回归上限
   （3 s / 150 MB / 6 s），能用结构断言的地方不装秒表。

## 为什么

**分发。** 旧安装说明要求用户自己装 Python 3.10+ 再 `pip install`，这是最大的一类
安装失败源；而应用的核心是 Python。备选方案与取舍：

- *PyInstaller onedir*：仍要为 Node 与浏览器另做下载，且冻结后的 `--doctor` 与
  子进程调试路径会变，收益不比内置解释器大；
- *首次运行自动建 venv*：系统没有 Python 时仍然失败，没有解决根本问题；
- *内置 CPython*：构建期一次下载、用户零安装，且运行的就是 CI 测过的解释器。

**为什么不做命令 RPC。** 2026-10-19 用代码核实过：高频读取（pools/accounts/
account/export）**已经**在常驻通道里；桌面侧只剩 4 处直接后端调用——inbox 抓取
（网络）、doctor（每进程一次）、任务协调器两条（带进度的长任务）。把长任务塞进
单线程读服务会阻塞读取，需要的是带 submit/progress/cancel 的任务服务，而不是命令
RPC；doctor 会把 playwright/curl_cffi 导入常驻进程，把 24 MB 的读服务抬到 60 MB+。
在出现「逐行调用网络命令」的消费者之前，建它只会得到一个没有调用方的抽象。

**为什么 worker 默认关闭。** Sentinel 是风控路径。worker 复用的是同一个 vendored
runner 与同一套校验，但“同一进程连续发 token”与“每次新进程”是运行形态差异，
按本仓库惯例（每个新行为先做单变量对比）先给开关，而非直接改默认。

**为什么内存阀是“回收”而不是“kill”。** 浏览器内存是进程树（浏览器 + renderer），
跨驱动没有稳定的 PID 句柄；汇总当前进程后代是可移植的近似。它是回收触发器：
超限时摘除一个空闲槽位并在下次取用时重建，正在使用的槽位不受影响。

## 证据

- `scripts/stage_python_runtime.ps1 -DryRun` → 固定 3.12.15 / 20261013 / SHA-256；
  发布载荷闸门要求 `RUNTIME-STAMP.json` 的摘要与清单一致。
- `pytest tests/registration/test_sentinel_worker.py` → 4 passed（复用 PID、判定
  失败不冷启动、传输失败只回落一次、默认关闭）。
- `pytest tests/registration/test_browser_pool.py` → 32 passed（含超限回收与
  未超限复用）。
- 入口实测：`import sms_tool.cli` 0.17–0.28 s / 24 MB；`--help` 0.7–1.1 s。

## 后果

- 发布构建必须先运行 staging 脚本；缺少运行时或 stamp 时构建失败，`-SkipBundledPython`
  只服务本地开发。
- Node.js 与浏览器二进制仍是外部依赖，`--doctor` 继续报告。
- worker 在受控实测证明无行为差异后再考虑改默认；任务服务的触发条件是出现逐行
  网络命令消费者，并同时具备进度与取消契约。

## 明确推迟

以下两项在本轮评估中被确认值得做、但体积不适合与运行形态同批落地：

1. **根目录基础件收敛到 `kernel/core`。** `sms_tool/` 根下 155 个模块混装基础件、
   领域件与适配器，子包反向依赖根包（`accounts -> sms_tool` 57 条边）。触发条件：
   下一次新增根级基础模块之前，或 import ratchet 的根包边数继续增长时；做法是每次
   只搬 10–12 个纯基础模块并下调对应基线。
2. **`MainWindow` partial 拆分。** 16 个 partial 里 `Register` 765 行、`Export` 713 行，
   已接近“分布式上帝对象”。触发条件：下一次需要改 Register/Pools/Export 的功能；
   届时按功能抽 ViewModel + 服务，partial 只留 XAML 接线。
