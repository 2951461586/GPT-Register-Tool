"""预检的并发探测契约（2026-09-18 并发化）。

背景：`preflight_registration_before_mailbox` 原先逐个候选串行探测，启动开销与
候选数成正比 —— 实测 10 个候选 **124s**。同一仓库的
`select_registration_proxy_pool` 早就在用 `ThreadPoolExecutor(max_workers=8)`
探同一个池子，所以串行是遗漏而非设计。

并发化必须**只**改「探测的发起顺序」，不能顺手动摇下面这些契约。本文件把它们
逐个钉住（计数与顺序的既有契约在 `test_registration_preflight_budget.py`）：

1. 不同出口**真的同时**在探（不是换了个写法的串行）；
2. 未验证 / 已失败的出口每轮在飞上限 = `per_host_limit`（保住「坏出口只吃 N 个
   探测」的守卫）；
3. 已证明健康的出口不再受该上限约束（否则单出口池拿不到提速）；
4. `successful_routes` 按**候选序**、不按完成序 —— 调用方取 `[0]` 当 `args.proxy`；
5. 单候选的重试参数（`proxy_attempts=2`）没被并发化顺手改掉。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool.commands.registration import _preflight_host_label


def _args(**overrides):
    values = {"proxy": "http://first.example:8080", "proxy_explicit": False, "proxy_pool": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**registration):
    return {"registration": {"driver": "protocol", **registration}, "proxy": {}}


def _one_host_pool(size: int) -> list[str]:
    """`size` 个候选，**共享同一个 host:port**（真实池就是靠 userinfo 区分 sid）。

    🔴 这里必须**断言夹具前提**，不能只断言结果：``_preflight_host_label`` 只在
    ``host:port`` 与 ``user:pass@host:port`` 两种形式下给出 ``host:port``；写成
    ``http://one.example:8080#0`` 会解析失败、退回**整串**，于是每个候选都成了
    「独立主机」—— 主机守卫与主机上限**全都测不到**，测试静默变成在测窗口上限。
    2026-09-18 实际踩到过：`peak` 报 8 而不是 7，一度被误读成实现的 bug。
    """
    pool = [f"http://user{n}:pass@one.example:8080" for n in range(size)]
    labels = {_preflight_host_label(candidate) for candidate in pool}
    assert labels == {"one.example:8080"}, labels
    return pool


def _run(pool, probe, config=None, monkeypatch=None):
    monkeypatch.setattr(cli, "CFG", config or _config())
    with patch.object(cli, "_proxy_pool_values", return_value=pool), patch(
        "sms_tool.registration.registration_network_preflight", side_effect=probe
    ):
        return cli._preflight_registration_before_mailbox(_args())


class _Concurrency:
    """Records how many probes were in flight at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.entered = 0

    def __enter__(self) -> int:
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            index = self.entered
            self.entered += 1
            return index

    def __exit__(self, *_exc) -> None:
        with self._lock:
            self.live -= 1


def test_distinct_hosts_are_probed_at_the_same_time(monkeypatch):
    """三个不同出口必须同时挂在探测里。

    用一个三方 barrier 做判据：串行实现会在第一个候选上等超时，barrier 破裂 ⇒
    探测全部失败 ⇒ 预检抛 `no_healthy_route`。所以这条测试**不会**靠"跑得快"
    蒙过去。
    """
    pool = [f"http://host-{n}.example:8080" for n in range(3)]
    # 夹具前提：三个候选必须落在三个**不同**主机上，否则这条测试在验证别的路径。
    assert len({_preflight_host_label(c) for c in pool}) == 3
    barrier = threading.Barrier(len(pool), timeout=5)
    meter = _Concurrency()

    def probe(proxy, **_kwargs):
        with meter:
            barrier.wait()
        return {"ok": True, "proxy": proxy}

    result = _run(pool, probe, _config(), monkeypatch)

    assert result["ok"] is True
    assert meter.peak == len(pool)


def test_an_unproven_host_is_capped_before_the_window_is_spent(monkeypatch):
    """单出口池全死时，探测数必须仍是 `per_host_limit`，不是窗口大小。

    这是 2026-09-13 那次 30 候选全死烧掉 10m51s 之后加的守卫；并发化不能把它
    变成「一轮探满 8 个」。
    """
    pool = _one_host_pool(10)
    meter = _Concurrency()

    def probe(proxy, **_kwargs):
        with meter:
            time.sleep(0.02)
            raise RuntimeError("connection_reset")

    try:
        _run(pool, probe, _config(preflight_max_consecutive_failures_per_host=3), monkeypatch)
    except RuntimeError as exc:
        assert str(exc).startswith("registration_preflight_failed:no_healthy_route:")
    else:  # pragma: no cover - 全死必须抛
        raise AssertionError("an all-dead pool must raise")

    assert meter.entered == 3, meter.entered
    assert meter.peak == 3, meter.peak


def test_a_proven_healthy_host_gets_the_whole_window(monkeypatch):
    """先按 cap 探一轮，证明健康后剩下的并发探完 —— 这才是提速的来源。

    10 个候选、cap=3、窗口 8 ⇒ 第一轮 3 个、第二轮 7 个 ⇒ **峰值并发 7**。
    若实现把已验证健康的出口也压在 cap 上，峰值只会是 3（要 4 轮才探完），
    这条断言就会红。
    """
    pool = _one_host_pool(10)
    meter = _Concurrency()

    def probe(proxy, **_kwargs):
        with meter:
            time.sleep(0.05)
        return {"ok": True, "proxy": proxy}

    result = _run(pool, probe, _config(preflight_max_consecutive_failures_per_host=3), monkeypatch)

    assert result["ok"] is True
    assert meter.entered == 10
    assert meter.peak == 7, meter.peak


def test_successful_routes_follow_candidate_order_not_completion_order(monkeypatch):
    """第一个候选完成得**最晚**，但它仍必须是 `args.proxy`。

    并发下完成序是随机的；`args.proxy` / `args.proxy_pool` 的语义是「按候选顺序
    取第一条/全部可用路由」，所以实现必须按序号归位，不能按完成序 append。
    """
    pool = [
        "http://slow.example:8080",
        "http://fast-a.example:8080",
        "http://fast-b.example:8080",
    ]
    finished: list[str] = []
    lock = threading.Lock()

    def probe(proxy, **_kwargs):
        time.sleep(0.25 if "slow" in proxy else 0.0)
        with lock:
            finished.append(proxy)
        return {"ok": True, "proxy": proxy}

    args = _args()
    monkeypatch.setattr(cli, "CFG", _config())
    with patch.object(cli, "_proxy_pool_values", return_value=pool), patch(
        "sms_tool.registration.registration_network_preflight", side_effect=probe
    ):
        result = cli._preflight_registration_before_mailbox(args)

    assert result["ok"] is True
    # 前置条件：完成序确实被打乱了（否则这条测试什么都没证明）。
    assert finished[-1] == pool[0], finished
    assert args.proxy == pool[0]
    assert args.proxy_pool.splitlines() == pool


def test_each_probe_still_asks_for_two_attempts(monkeypatch):
    """并发化只跨候选；单候选内部的重试参数必须原样传下去。"""
    pool = ["http://host-0.example:8080", "http://host-1.example:8080"]
    seen: list[object] = []
    lock = threading.Lock()

    def probe(proxy, **kwargs):
        with lock:
            seen.append(kwargs.get("proxy_attempts"))
        return {"ok": True, "proxy": proxy}

    _run(pool, probe, _config(), monkeypatch)

    assert seen == [2, 2]
