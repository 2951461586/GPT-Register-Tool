r"""对 ``registration_drivers/browser_flow`` 子包内的**多副本符号**打补丁。

## 为什么需要它

拆分前 ``registration_drivers/playwright.py`` 是单一模块，所有 helper 都住在同一个
模块全局里。``patch("...playwright._xxx")`` 改的是那个唯一的全局属性，
**任何调用点读到的都是它** —— patch 一次，全局生效。

拆成 ``browser_flow/`` 子包后，每个子模块各自执行
``from .dom_fields import _first_visible``，拿到的是**自己模块命名空间里的一份引用**。
于是::

    patch("sms_tool.registration_drivers.browser_flow.orchestrator._wait_for_registration_state")

只改掉了 orchestrator 的那份；``form_steps.py`` 里那份仍指向真实函数。patch **静默失效**：

* 不抛异常 —— 属性确实存在，patch 成功；
* 测试却走到真实逻辑，典型症状是「抛了意料之外的 ``BrowserRegistrationError``」
  或「mock 的 ``call_args`` 是 ``None``」。

单独跑这个文件是绿的、全量跑才红的情况也由此而来：副本是否被走到取决于调用路径。

## 用法

与 ``unittest.mock.patch`` 同构，装饰器与上下文管理器都支持::

    from browser_flow_patch import patch_bf

    @patch_bf("_wait_for_registration_state", return_value="otp")
    def test_something(self, wait_state):
        ...

    with patch_bf("_wait_for_registration_state", return_value="otp") as wait_state:
        ...

对所有副本装的是**同一个** mock 对象，因此 ``call_args`` / ``call_count`` 反映跨模块的
**合并**调用记录，与拆分前的语义一致。

## 设计取舍

副本表**不硬编码**，而是运行时用 introspection 算（遍历包 + 子模块，凡有该属性即副本）。
硬编码表会在下次拆分/移动函数时悄悄过时，而那时的失效又是一次静默失败。
``copy_targets()`` 是公开函数，配套守卫测试会拿它和 AST 扫描结果对账。
"""

from __future__ import annotations

import functools
from contextlib import ExitStack
from types import ModuleType
from unittest.mock import DEFAULT, MagicMock, patch

__all__ = ["patch_bf", "copy_targets"]

_PKG = "sms_tool.registration_drivers.browser_flow"


def _submodules():
    """包本身 + 所有已导入的子模块。

    只认**已导入**的：没被 import 的模块不可能持有副本，
    而 patch 一个尚未存在的模块只会引入误报。
    """
    import importlib

    pkg = importlib.import_module(_PKG)
    mods = [pkg]
    for key, value in list(vars(pkg).items()):
        if isinstance(value, ModuleType) and getattr(value, "__name__", "").startswith(_PKG + "."):
            mods.append(value)
    return mods


def copy_targets(name: str) -> list[str]:
    """返回 ``name`` 在 browser_flow 里的全部副本，形如 ``<模块dotted>.<name>``。"""
    targets = []
    for mod in _submodules():
        if hasattr(mod, name):
            targets.append(f"{mod.__name__}.{name}")
    return sorted(set(targets))


class _PatchBF:
    """``unittest.mock.patch`` 的 drop-in 替代，但同时打上所有副本。"""

    def __init__(self, name, new=DEFAULT, **kwargs):
        if not isinstance(name, str) or "." in name:
            raise ValueError(
                f"patch_bf 只接受裸符号名（如 '_wait_for_registration_state'），收到 {name!r}。"
                " 带点的目标请用 unittest.mock.patch。"
            )
        self.name = name
        self.new = new
        self.kwargs = kwargs
        self._stack = None
        self._mock = None
        self.patchings = [self]  # 与 mock.patch 保持形状一致，便于调试

        # --- 与 unittest.mock.patch 混用时的兼容属性 -------------------------
        # 当同一组装饰器里既有 patch() 又有 patch_bf() 时，wrapper 由**最内层**
        # 装饰器创建。若最内层是 patch()，它会遍历 patchings 并读取
        # `attribute_name`：非 None 就走 `keywargs.update(arg)`（把 mock 当
        # patch.multiple 的字典），而那不是我们的语义 —— 必须给 None，
        # 让它走 `elif patching.new is DEFAULT: extra_args.append(arg)` 分支。
        self.attribute_name = None

    # -- 上下文管理器 --------------------------------------------------
    def __enter__(self):
        targets = copy_targets(self.name)
        if not targets:
            raise AttributeError(
                f"browser_flow 里找不到符号 {self.name!r} —— 名字写错了，还是它被移走了？"
            )
        if self.new is not DEFAULT:
            shared = self.new
        elif self.kwargs.get("new", DEFAULT) is not DEFAULT:
            shared = self.kwargs["new"]
        else:
            kw = {k: v for k, v in self.kwargs.items() if k != "new"}
            shared = MagicMock(name=self.name, **kw)
        self._stack = ExitStack()
        # 同一个 shared 对象装到每个副本上 -> call_args 是跨模块的合并记录
        for target in targets:
            self._stack.enter_context(patch(target, shared))
        self._mock = shared
        return shared

    def __exit__(self, *exc_info):
        return self._stack.__exit__(*exc_info)

    # -- 装饰器 --------------------------------------------------------
    def __call__(self, func):
        if isinstance(func, (str, bytes)):
            raise TypeError("patch_bf 不支持 patch_bf(...)(target) 这种间接形式")
        if isinstance(func, type):
            raise TypeError("patch_bf 目前不支持装饰类")

        # 必须复刻 unittest.mock.patch 的 patchings 累积机制：
        # 最**内层**装饰器先建 wrapper 并把 patchings 设成 [自己]，
        # 外层装饰器发现 func 已有 patchings 就 append 自己。
        # 于是 extras 顺序是「由内到外」——即最内层装饰器的 mock 是**第一个**参数。
        #
        # 直觉上容易写成「外层先追加」，那样多个装饰器的 mock 会整体错位一位：
        # 症状是 return_value 配到别的 mock 上、断言拿到别的符号的调用记录，
        # 且因为是 MagicMock（什么都接受），不会报错，只会安静地断言失败。
        if hasattr(func, "patchings"):
            func.patchings.append(self)
            return func

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            extras = []
            with ExitStack() as stack:
                for p in wrapper.patchings:
                    extras.append(stack.enter_context(p))
                return func(*(args + tuple(extras)), **kwargs)

        wrapper.patchings = [self]
        return wrapper

    def start(self):
        raise NotImplementedError("patch_bf 不支持 start()/stop()，请用 with 或装饰器")

    def stop(self):  # pragma: no cover - 与 start 配对
        raise NotImplementedError("patch_bf 不支持 start()/stop()，请用 with 或装饰器")


def patch_bf(name, new=DEFAULT, **kwargs):
    """见模块 docstring。"""
    return _PatchBF(name, new=new, **kwargs)
