"""heavy lane 容量不变量（round3 P2-15）。

``account_recovery._heavy_lane_slots`` 必须保证：**只要 worker 池 >= 2，
heavy lane 就严格小于池大小**。

heavy lane（browser fallback / 401 relogin）存在的意义是给便宜的 HTTP probe
留余量。一旦它的容量等于池大小，慢恢复就能占满所有 worker，
``browser_slots`` / ``relogin_slots`` 两个 semaphore 形同虚设 ——
而 acquire 是阻塞式的（``account_recovery.py:172/222``），所以表现不是"丢账号"，
而是**轻 probe 排队等重活**。

旧公式 ``max(1, min(max_workers, max(2, max_workers // 2)))`` 里的 ``max(2, ...)``
下限在小并发下会反向压过 ``max_workers``：``max_workers=2`` 算出来是 2。
而 **2 正是配置默认值**（``account_health_queue.py:121`` 的
``int(health.get("workers") or 2)``）—— 默认配置下这个隔离从来没生效过。
"""

import pytest

from sms_tool.accounts.account_recovery import _heavy_lane_slots


# (max_workers, expected) —— 覆盖边界（1/2/3）与大池等比放大
_EXPECTED = {
    1: 1,
    2: 1,
    3: 1,
    4: 2,
    5: 2,
    6: 3,
    7: 3,
    8: 4,
    12: 6,
    16: 8,
}


@pytest.mark.parametrize("max_workers, expected", sorted(_EXPECTED.items()))
def test_expected_capacity(max_workers, expected):
    assert _heavy_lane_slots(max_workers) == expected


@pytest.mark.parametrize("max_workers", range(2, 33))
def test_strictly_narrower_than_the_pool(max_workers):
    """核心不变量 —— 池 >= 2 时 heavy lane 必须严格小于池。"""
    slots = _heavy_lane_slots(max_workers)
    assert 1 <= slots < max_workers


def test_single_worker_pool_has_no_room_to_isolate():
    """1 个 worker 时物理上无法隔离，但必须返回 1 —— 返回 0 会让所有重活直接 skip。"""
    assert _heavy_lane_slots(1) == 1


def test_degenerate_pools_never_return_zero():
    """负向测试：0 / 负数池不能算出 0 或负数容量。"""
    for max_workers in (-5, 0, 1):
        assert _heavy_lane_slots(max_workers) >= 1


def test_capacity_scales_with_large_pools():
    """负向测试：不能退化成常量。大池必须等比放大，否则并发上不去。"""
    assert _heavy_lane_slots(32) > _heavy_lane_slots(8)
    assert _heavy_lane_slots(8) > _heavy_lane_slots(2)
