r"""ChatGPT registration through a native Playwright browser session.

实现已按职责拆到 ``registration_drivers/browser_flow/`` 子包，由低到高：:

    dom_fields  ->  page_state  ->  form_steps  ->  flow_steps  ->  orchestrator
                                                  \-> session

本模块是**公共 API 薄壳**，只 re-export 三个入口符号。

设计取舍：这里刻意**不** re-export 内部 ``_*`` 辅助函数。原因是本仓历史上所有
``patch("sms_tool.registration_drivers.playwright._xxx")`` 都依赖"调用点与被调函数
同在一个模块全局"这一巧合；薄壳一旦 re-export 私有符号，这类 patch 会**静默失效**
（名字存在、但打不到真正的调用点），单独跑还绿、全量跑才红。只导出公共 API 会让
残留的旧 patch 目标直接抛 ``AttributeError``，**响亮失败**而不是悄悄放过。
"""

from .browser_flow import (
    build_browser_session_file,
    run_browser_registration,
    run_playwright_registration,
)

__all__ = [
    "run_browser_registration",
    "run_playwright_registration",
    "build_browser_session_file",
]
