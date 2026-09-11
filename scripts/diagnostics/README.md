# Diagnostics archive

一次性事故诊断脚本（camoufox 启动、roxy 出口、remail 顺序、凭据轮换等）。
它们以 `_` 前缀命名，被 `.gitignore` 的 `_*.py` 规则忽略——按仓库规范这是
本地临时脚本的位置。本目录是它们的本地归档：不维护、不进发布载荷、不进 CI；
引用的接口随版本演进可能已失效。需要可复用化的探测/诊断，应改写为
`scripts/` 下的正式脚本（带测试）或 `sms_tool/doctor.py` 的检查项。
