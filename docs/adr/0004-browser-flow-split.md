# ADR-0004: playwright.py 拆分为 browser_flow 分层包

- Status: Accepted
- Date: 2026-09-05（2026-09-06 修订：消除 patch 失效面）

原 `sms_tool/registration_drivers/playwright.py`（2035 行单文件）按职责分层
拆为 `sms_tool/registration_drivers/browser_flow/`，依赖单向：

    dom_fields（独立 DOM 原语）
      → page_state（页面状态判定与等待）
        → form_steps（表单语义步骤）
          → flow_steps（会话作用域/进程池、OTP 轮询、流程重启）
            → orchestrator（入口编排）
                                ↘ session（session payload/AT 探测/2FA）

`playwright.py` 退化为公共 API 薄壳，只 re-export
`run_browser_registration` / `run_playwright_registration` /
`build_browser_session_file`。

## 2026-09-06 修订：调用点走定义模块命名空间

拆分初期各层用 from-import 持有彼此的私有函数副本，同一函数存在多个命名空间
副本，`mock.patch` 打任何单个副本都静默失效（单独跑绿、全量跑红），仓库被迫
维护 170 行测试专用 `tests/browser_flow_patch.py` 多副本打补丁器 + 203 行
AST/运行时守卫测试。修订后：

- `browser_flow` 内部跨模块调用一律写 `form_steps._fill_email(...)` 形式，
  禁止 from-import 私有函数；
- `browser_flow/__init__.py` 只导出三个公共入口，不再 re-export 私有符号；
- `tests/browser_flow_patch.py` 与 `tests/test_browser_flow_patch_helper.py`
  删除，测试直接 patch 源模块；
- `run_browser_registration` 的 `session_factory` 默认参数改为调用时解析
  （`None` → `external_sessions.create_browser_session`），默认参数绑定
  不再固化函数对象。

## Consequences

- patch 只能落在源模块，打错位置响亮失败而非静默放过。
- 编排函数（`run_browser_registration` 约 565 行）的函数级拆分留待后续。
