# ADR-0008: 注册结果契约统一到 registration_result

- Status: Accepted
- Date: 2026-09-06

协议路径（`registration_handlers.finalize`）与浏览器路径
（`browser_flow/orchestrator.run_browser_registration`）的 result dict 共享
约 28 个核心键。历史上两条路径各手写一份 dict 字面量，键集合只靠注释保持
同步（orchestrator 注释原话是 "mirror the protocol path"），漂移只能靠
下游测试兜底。

现在共享装配收敛到 `sms_tool/registration_result.build_registration_result`：

- `COMMON_RESULT_KEYS` 定义两条路径都必须提供的核心键契约；
- 两条路径把**已计算好的值**传入（成功判定仍归
  `registration_outcome._registration_outcome` / `_probe_registration_access_token`，
  本模块不重复判定），路径专有键（协议的 `quota`/`timing`/`sentinel_version`
  等，浏览器的 `registration_driver`/`browser_diagnostics`/`proxy_audit`/
  `driver_capabilities` 等）经 `extra` 合入；
- 自由文本字段（error / registration_warning）在装配点统一过
  `sanitizer.sanitize_text`；
- 装配器对缺失核心键直接抛错（`COMMON_RESULT_KEYS` 集合校验）。

`tests/test_registration_result_contract.py` 同时做单元断言与 AST 守卫：
两个生产文件各恰好一次 builder 调用，且不允许重新出现赋值给 `result` 的
手写装配字面量。

## Consequences

- 新增共有键：先改 `COMMON_RESULT_KEYS`，再同时补两条路径的实参，
  契约测试会在任何一侧遗漏时失败。
- 密码脱敏策略保持路径差异：协议成功结果输出明文密码（本地持久化需要），
  浏览器路径仅在 `password_used` 时输出；这属于调用方决策，不归装配器。
