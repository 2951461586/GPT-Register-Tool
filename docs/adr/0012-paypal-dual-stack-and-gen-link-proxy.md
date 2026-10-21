# ADR-0012: PayPal 双栈边界与 `paypal_link.gen_link` 的 `_PlmProxy`

- Status: Accepted
- Date: 2026-10-18

## 决定

1. **两个栈不合并。** `sms_tool/paypal/` 是浏览器执行栈（编排 / flow / form /
   session / DOM / config / errors 七层，按 Rule 5 单向无环）；`sms_tool/paypal_link/`
   是 Checkout 提链与商户返回对账栈（`gen_link.py` / `reconciliation.py`）。
   两者共享的是 `payment_wire.py` 的支付内核（Rule 20），不是彼此。
2. **`_PlmProxy` 保留。** `paypal_link/gen_link.py` 用 `__getattr__` 在属性访问时
   从 `sys.modules` 读取 `sms_tool.gen_pp_link` 的活属性。它不是未清理的残留。
3. **新增调用点直接 import 定义模块**（`paypal_link` / `paypal_extract` /
   `payment_wire`），不得给两个根壳（`gen_pp_link.py`、`paypal/reconciliation.py`）
   增加新依赖。壳只服务既有 import 路径与既有 patch 目标。

## 为什么需要记录

读者看到的形状像三份重复：`paypal/`（8 文件）、`paypal_link/`（3 文件）、根
`paypal/extract.py` + 两个壳。2026-10-18 的深度扫描把 `_PlmProxy` 标为“机械拆分
留下的间接层”，建议清理。**这个建议是错的**，而且错得静默：删掉它以后
`patch.object(sms_tool.gen_pp_link, ...)` 仍然能安装补丁，但子模块再也读不到补丁后的
属性——测试会继续变绿，因为“补丁了一个没人读的属性”不会报错。

不记录这条，下一个人删它的理由与本次扫描完全一样。

## 证据

- `paypal_link/gen_link.py` 里有 **14 处** `_plm.X`，覆盖 `PPLinkExtractor`、
  `_load_json`、`_proxies_from_config`、`_checkout_post` 等；`PPLinkExtractor` 定义在
  `sms_tool/paypal/extract.py:169`，`_checkout_post` 定义在 `sms_tool/payment_wire.py`。
  两者都不是 `gen_pp_link` 的所有物——壳只做再导出。
- `tests/` 中对 `gen_pp_link` 的引用有 **140 行**，其中
  `patch.object(gen_pp_link, "_checkout_post" / "_checkout_get", ...)` 是活跃的 patch
  目标（`tests/accounts/test_account_payment_eligibility.py:194`、
  `tests/payment/test_checkout_sentinel.py:156` 等）。这些补丁今天能生效，正是因为
  `_PlmProxy` 在**调用时**读取模块属性，而不是在 import 时绑定。
- 两个壳是单向再导出：`gen_pp_link.py` → `paypal_link.*` 加 `upi_link` 遗留符号；
  `paypal/reconciliation.py` → `paypal_link.*`。
- 执行栈与提链栈的方向约束是既有的：Rule 6 禁止 `sms_tool/paypal/` import
  `gen_pp_link`；反过来 `paypal_link/` 也不驱动浏览器，只产出链接与对账证据。

## 后果

- **新代码**：`from .paypal_link import ...` 或 `from .payment_wire import ...`。
  不要 `from .gen_pp_link import ...`；两个壳的 `__all__` 只保证旧路径不炸。
- **`_PlmProxy` 的退出条件**（满足任意一条才可另立项删除）：
  1. `grep -rn "gen_pp_link" tests/` 不再命中 `patch.object(gen_pp_link, ...)`；
  2. 仓库内不再有 `sms_tool.gen_pp_link.X` 形式的运行时读取。
  删除属于一次依赖图变更（Rule 19：同名/同义不等于可合并），需要独立证据与决策，
  不能夹在“清理”里完成。
- **检查**：`grep -rn "gen_pp_link" sms_tool/paypal/` 必须为空（Rule 6）；
  `grep -c "_plm\." sms_tool/paypal_link/gen_link.py` 的增长需要说明新增的读取点。
