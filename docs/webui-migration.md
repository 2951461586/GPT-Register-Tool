# WebUI 迁移文档 (M6-M8)

## 概述

M6-M8 将 WPF 桌面应用的共享契约提取为独立项目，并在此基础上构建了 ASP.NET Core 本地 WebHost 和 React 工作台 MVP。WPF 和 WebUI 共用同一套 CLI Planner、IPC 协议、任务协调接口和结果类型，Python 后端不感知调用方是 WPF 还是 WebHost。

## M6: 共享 Contracts

### 新增项目

`SmsWorkbench.Contracts/` — `net10.0` 类库，无 WPF 依赖。

包含的类型（命名空间保持 `SmsWorkbench`，WPF 源码无需 `using` 变更）:

| 文件 | 类型 |
|---|---|
| `BackendContracts.cs` | `BackendOutputChannel`, `BackendOutputLine`, `BackendCommand`, `BackendCommandResult`, `IBackendClient` |
| `BackendCommandPlanner.cs` | `BackendCommandPlan`, `BackendCommandPlanner` |
| `BackendJsonProtocol.cs` | `BackendJsonProtocol` (v2 信封解析) |
| `BackendProgressEvents.cs` | `BackendProgressEvent`, `BackendProgressEventParser` |
| `BackendJson.cs` | `BackendJson` (JSON → Dictionary 投影) |
| `BackendTaskContracts.cs` | `IBackendTaskCoordinator`, `BackendTaskAlreadyRunningException` |
| `BackendResultContracts.cs` | `ProxyTestStageResult`, `ProxyTestResult`, `BackendExecutionResult` |
| `MailboxCredentialLineParser.cs` | iCloud URL 行解析（从 `MailboxPoolFileStore` 抽取的纯函数） |

### WPF 项目保留的实现

- `BackendTaskCoordinator` 实现（依赖 `SensitiveDataSanitizer`）
- `BackendResultInterpreter`（依赖 `SensitiveDataSanitizer`，含中文展示文本）
- `PythonBackendClient`（进程管理 + Settings）
- `SensitiveDataSanitizer`（嵌入资源）
- `MailboxPoolFileStore`（导入/导出/原子替换）

### 项目引用

```
SmsWorkbench.Contracts ← SmsWorkbench
SmsWorkbench.Contracts ← SmsWorkbench.Tests
SmsWorkbench.Contracts ← SmsWorkbench.WebHost
```

## M7: WebHost + React MVP

### SmsWorkbench.WebHost

ASP.NET Core Minimal API，`net10.0`，仅监听 `127.0.0.1`。

#### 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/meta` | 服务元信息（paymentEnabled = false） |
| GET | `/api/accounts` | 账号列表，支持 q/status/planType/promotionStatus 筛选 + 分页 |
| GET | `/api/accounts/{id}` | 单账号详情 |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/{id}` | 任务状态 |
| POST | `/api/jobs/registrations` | 注册任务（pool/phone/cfworker/remail/smailr） |
| POST | `/api/jobs/account-health` | 深度测活 |
| POST | `/api/jobs/account-promotions` | 套餐/优惠检测 |
| POST | `/api/jobs/accounts/{id}/quota-usage` | 额度查询 |
| POST | `/api/jobs/{id}/cancel` | 取消任务 |
| GET | `/api/jobs/{id}/events` | SSE 事件流（state/progress/log） |

#### 安全

- Kestrel 仅绑定 `127.0.0.1`，非环回 Host 返回 400
- 随机生成 per-launch session token，HttpOnly cookie 验证
- POST 请求校验同源 Origin
- DTO 白名单排除 `session`、`json_path`、`device_id`、支付相关字段
- 日志通过正则脱敏 `access_token`/`refresh_token`/`cookie`/`password`/`api_key` 等

#### 单写任务队列

`BackendJobManager` 全局只允许一个活跃写任务，第二个提交返回 409 Conflict。读操作通过 `PythonAccountCatalog` 调用 `--desktop-read accounts --desktop-ipc`，与写任务互不阻塞。

#### Python CLI 变更

注册、测活、套餐、额度命令在 `--desktop-ipc` 模式下通过 `emit_result()` 输出 v2 结果信封，使 WebHost 可通过 `BackendJsonProtocol.ExtractPayload()` 解析结构化结果。

### React 工作台 (`webui/`)

React 19 + TypeScript + Vite，构建输出到 `SmsWorkbench.WebHost/wwwroot/`。

#### MVP 功能

- 账号表格：搜索、筛选（状态/套餐/优惠）、分页
- 注册任务提交：选择来源（邮箱池/手机号/CFWorker/ReMail/Smailr）、数量、并发
- 账号健康：深度测活、套餐/优惠检测、额度查询
- 任务列表 + 实时 SSE 日志
- 任务取消
- 浅色/深色主题（CSS Variables 对齐 WPF 设计令牌）
- 可折叠侧栏

#### 视觉映射

WPF XAML 设计令牌 → CSS Variables:

```css
--surface: #ffffff;        /* AppBg / PanelBg */
--surface-secondary: #f3f3f3;  /* PanelBg2 / SidebarBg */
--border: #e2e2e2;         /* Line */
--primary: #2563eb;        /* Primary */
--danger: #b42318;         /* Danger */
--success: #16794c;        /* Success */
--radius: 4px;             /* CornerRadius */
```

#### 不包含

- 支付操作（高后果操作，不进入首版）
- 邮箱池/收件箱管理
- 配置编辑
- 导入导出

### 开发模式

```bash
# 启动 WebHost (端口 5137)
dotnet run --project SmsWorkbench.WebHost

# 启动 Vite dev server (端口 5173, 代理 /api → 5137)
cd webui && npm run dev
```

### 发布模式

WebHost 的 `publish` 目标会自动执行 `npm install && npm run build`，将前端构建到 `wwwroot/`。

```bash
dotnet publish SmsWorkbench.WebHost -c Release -o dist/webhost
```

## M8: 双端契约测试

### SmsWorkbench.WebHost.Tests

| 测试文件 | 覆盖内容 |
|---|---|
| `RegistrationCommandFactoryTests.cs` | 注册 DTO → CLI 参数映射、CFWorker 域校验、代理注入、count 边界 |
| `AccountCatalogTests.cs` | 账号 DTO 字段白名单（排除 session/json_path/device_id）、缺失字段容错 |
| `BackendJobManagerTests.cs` | 任务生命周期（启动→完成/失败/取消）、并发冲突 409、进度事件捕获 |
| `ServerCommandDefaultsTests.cs` | config.json 解析（Python 路径/代理池/域名）、缺失配置回退 |

### 验证结果

| 套件 | 结果 |
|---|---|
| Python (pytest) | 1140 passed, 72 subtests passed |
| .NET WPF (xUnit) | 220 passed |
| .NET WebHost (xUnit) | 18 passed |
| Frontend (vitest) | 3 passed |
| WPF 发布 | `dist/net10/SmsWorkbench.exe` ✓ |
| WebHost 发布 | `dist/webhost/SmsWorkbench.WebHost.exe` ✓ |
