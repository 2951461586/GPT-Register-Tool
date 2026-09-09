r"""ChatGPT 注册浏览器流程实现（Playwright / 反检测浏览器共用编排）。

历史上全部实现都在 ``registration_drivers/playwright.py``（2035 行单文件），
2026-09-05 按职责分层拆开，由低到高：

    dom_fields  ->  page_state  ->  form_steps  ->  flow_steps  ->  orchestrator
                                                  \-> session

本包 ``__init__`` 只 re-export 三个公共入口符号。

设计取舍（2026-09-06 起生效）：这里刻意**不**再 re-export 内部 ``_*`` 辅助函数。
原因是本仓历史上所有 ``patch("...browser_flow._xxx")`` 都依赖"调用点与被调函数
同在一个模块全局"这一巧合；包级 re-export 制造了同一函数的多个命名空间副本，
patch 必须打遍所有副本才生效（曾经需要 tests/browser_flow_patch.py 整套多副本
打补丁机制来补救）。现在各层调用点一律走**定义模块的命名空间**
（``form_steps._fill_email(...)``），patch 只需、也只能打在源模块上——打错位置
会响亮地 AttributeError，而不是静默放过。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import (
        build_browser_session_file,
        run_browser_registration,
        run_playwright_registration,
    )

__all__ = ["run_browser_registration", "run_playwright_registration", "build_browser_session_file"]


def __getattr__(name: str):
    """Load the orchestration layer only when its public API is requested.

    Leaf modules such as ``browser_session`` import ``browser_flow.decisions``.
    Eagerly importing the package's orchestrator from here pulls ``session``
    back into ``browser_session`` and creates a circular import during module
    initialisation.  Lazy exports preserve the public API without making every
    leaf import the whole browser workflow.
    """
    if name in __all__:
        from . import orchestrator

        return getattr(orchestrator, name)
    raise AttributeError(name)
