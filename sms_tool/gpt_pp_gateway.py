#!/usr/bin/env python3
"""gpt-pp Go 网关客户端 — 高并发 PayPal 授权链接提取。

通过 HTTP API 调用本地 Go 网关实现并发提链，替代 Python ThreadPoolExecutor 逐个调用模式。

Go 网关提供：
  - POST /api/extract       单次提取（128 并发队列）
  - POST /api/extract-batch  批量提取（12 worker + NDJSON 流式响应）
  - GET  /api/health         健康检查
  - POST /api/test-proxy     代理连通性测试
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Generator

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
GATEWAY_SOURCE_DIR = PROJECT_ROOT / "scripts" / "gpt_pp_gateway"
GATEWAY_BINARY_UNIX = PROJECT_ROOT / "ppgateway"
GATEWAY_BINARY_WIN = PROJECT_ROOT / "ppgateway.exe"

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_ADDR = "127.0.0.1:8787"
DEFAULT_BATCH_CONCURRENCY = 12
HEALTH_TIMEOUT = 5
EXTRACT_TIMEOUT = 120
BATCH_LINE_TIMEOUT = 120
STARTUP_WAIT_SECONDS = 15
STARTUP_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _gateway_binary() -> Path:
    """返回网关二进制文件路径（根据平台）。"""
    if sys.platform == "win32":
        return GATEWAY_BINARY_WIN
    return GATEWAY_BINARY_UNIX


def _is_binary_fresh() -> bool:
    """检查二进制是否比源码新。"""
    binary = _gateway_binary()
    if not binary.exists():
        return False
    bin_mtime = binary.stat().st_mtime
    go_sources = list(GATEWAY_SOURCE_DIR.rglob("*.go"))
    if not go_sources:
        return True
    newest_source = max(f.stat().st_mtime for f in go_sources)
    return bin_mtime >= newest_source


def _normalize_gateway_result(raw: dict[str, Any]) -> dict[str, Any]:
    """将 Go 网关响应映射为 sms_tool 内部使用的统一结果格式。"""
    ok = bool(raw.get("ok"))
    paypal_url = str(raw.get("paypal_authorize_url") or "")
    hosted_url = str(raw.get("hosted_checkout_url") or "")
    amount_due = raw.get("amount_due")
    currency = str(raw.get("currency") or "").upper()
    code = str(raw.get("code") or "")
    message = str(raw.get("message") or "")

    # terminal / retryable 推断
    terminal_codes = {
        "checkout_unauthorized", "checkout_forbidden", "checkout_not_zero_due",
        "checkout_guard_failed", "paypal_not_supported",
    }
    terminal = code in terminal_codes or (400 <= int(raw.get("status") or 0) < 500)
    retryable = not terminal

    return {
        "ok": ok,
        "url": paypal_url,
        "stripe_redirect_url": paypal_url,
        "checkout_url": hosted_url,
        "hosted_checkout_url": hosted_url,
        "provider_url": hosted_url,
        "link_type": "gpt_pp_paypal_authorize",
        "source": "gpt_pp_gateway",
        "method": "paypal",
        "payment_method": "paypal",
        "cs_id": "",
        "session_id": "",
        "pm_id": "",
        "due": amount_due,
        "amount_due": amount_due,
        "currency": currency,
        "expected_amount": str(amount_due if amount_due is not None else 0),
        "zero_due_verified": bool(raw.get("zero_verified")),
        "amount_display": str(raw.get("amount_display") or "unknown"),
        "has_paypal": True,
        "link_mode": "stripe_redirect",
        "redirect_url_format": "stripe_authorize",
        "region": "",
        "billing_country": "",
        "proxy": str(raw.get("proxy_scheme") or ""),
        "elapsed_ms": int(raw.get("elapsed_ms") or 0),
        "error": "" if ok else message,
        "error_code": "" if ok else code,
        "terminal": terminal if not ok else False,
        "retryable": retryable if not ok else False,
        "gateway_raw": raw,
    }


# ---------------------------------------------------------------------------
# GptPpGateway 类
# ---------------------------------------------------------------------------

class GptPpGateway:
    """gpt-pp Go 网关客户端。

    支持三种模式：
    1. auto_start=True（默认）：自动编译、启动、管理子进程生命周期
    2. auto_start=False：仅作为 HTTP 客户端连接已有网关
    3. 手动调用 build() / start() / stop()
    """

    def __init__(
        self,
        addr: str = DEFAULT_ADDR,
        auto_start: bool = True,
        source_dir: str | Path | None = None,
        country: str = "US",
        currency: str = "USD",
        allow_non_zero: bool = True,
        timeout: int = 60,
        extra_args: list[str] | None = None,
    ):
        self.addr = addr
        self.auto_start = auto_start
        self.source_dir = Path(source_dir) if source_dir else GATEWAY_SOURCE_DIR
        self.country = country
        self.currency = currency
        self.allow_non_zero = allow_non_zero
        self.timeout = timeout
        self.extra_args = extra_args or []
        self._process: subprocess.Popen | None = None
        self._started_by_us = False

    @property
    def base_url(self) -> str:
        return f"http://{self.addr}"

    # ---- 生命周期 ----

    def ensure_running(self) -> bool:
        """确保网关正在运行。返回 True 表示就绪。"""
        if self.health().get("ok"):
            return True
        if not self.auto_start:
            return False
        if not self.build():
            logger.error("Go 网关编译失败")
            return False
        return self.start()

    def build(self) -> bool:
        """编译 Go 网关二进制。"""
        if _is_binary_fresh():
            logger.debug("Go 网关二进制已是最新，跳过编译")
            return True
        src = self.source_dir
        if not (src / "go.mod").exists():
            logger.error("Go 网关源码目录不存在: %s", src)
            return False
        if not shutil.which("go"):
            logger.error("未找到 Go 编译器，请先安装 Go: https://go.dev/dl/")
            return False
        logger.info("正在编译 Go 网关 (%s) ...", src)
        try:
            result = subprocess.run(
                ["go", "build", "-o", str(_gateway_binary()), "./cmd/ppgateway/main.go"],
                cwd=str(src),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error("Go 编译失败:\n%s", result.stderr)
                return False
            logger.info("Go 网关编译成功: %s", _gateway_binary())
            return True
        except subprocess.TimeoutExpired:
            logger.error("Go 编译超时")
            return False
        except FileNotFoundError:
            logger.error("未找到 Go 编译器")
            return False

    def start(self) -> bool:
        """启动 Go 网关子进程。"""
        binary = _gateway_binary()
        if not binary.exists():
            logger.error("网关二进制不存在: %s", binary)
            return False
        cmd = [
            str(binary),
            "-addr", self.addr,
            "-timeout", str(self.timeout),
        ]
        cmd.extend(self.extra_args)
        logger.info("启动 Go 网关: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._started_by_us = True
        except OSError as exc:
            logger.error("启动 Go 网关失败: %s", exc)
            return False

        # 等待健康检查通过
        deadline = time.monotonic() + STARTUP_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.health().get("ok"):
                logger.info("Go 网关已就绪 (pid=%s)", self._process.pid)
                return True
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode(errors="replace") if self._process.stderr else ""
                logger.error("Go 网关启动后立即退出 (code=%s): %s", self._process.returncode, stderr[:500])
                self._process = None
                self._started_by_us = False
                return False
            time.sleep(STARTUP_POLL_INTERVAL)
        logger.error("Go 网关启动超时 (%ss)", STARTUP_WAIT_SECONDS)
        self.stop()
        return False

    def stop(self) -> None:
        """停止 Go 网关子进程。"""
        if self._process is None:
            return
        if self._started_by_us and self._process.poll() is None:
            logger.info("正在停止 Go 网关 (pid=%s) ...", self._process.pid)
            try:
                if sys.platform == "win32":
                    self._process.terminate()
                else:
                    self._process.send_signal(signal.SIGTERM)
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=3)
            except OSError:
                pass
        self._process = None
        self._started_by_us = False

    def __enter__(self):
        self.ensure_running()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ---- API 调用 ----

    def health(self) -> dict[str, Any]:
        """GET /api/health"""
        try:
            resp = requests.get(f"{self.base_url}/api/health", timeout=HEALTH_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"ok": False}

    def extract(self, access_token: str, proxy: str = "") -> dict[str, Any]:
        """POST /api/extract — 单次提取。

        返回统一格式的结果字典。
        """
        payload: dict[str, Any] = {"accessToken": access_token}
        if proxy:
            payload["proxy"] = proxy
        try:
            resp = requests.post(
                f"{self.base_url}/api/extract",
                json=payload,
                timeout=EXTRACT_TIMEOUT,
            )
            raw = resp.json() if resp.status_code == 200 else {
                "ok": False,
                "code": f"http_{resp.status_code}",
                "message": resp.text[:300],
            }
        except requests.Timeout:
            raw = {"ok": False, "code": "gateway_timeout", "message": "Go 网关请求超时"}
        except requests.ConnectionError:
            raw = {"ok": False, "code": "gateway_unreachable", "message": "无法连接 Go 网关"}
        except Exception as exc:
            raw = {"ok": False, "code": "gateway_error", "message": str(exc)[:300]}
        return _normalize_gateway_result(raw)

    def extract_batch(
        self,
        tokens: list[str],
        proxy: str = "",
    ) -> Generator[dict[str, Any], None, None]:
        """POST /api/extract-batch — 批量 NDJSON 流式提取。

        每次 yield 一个统一格式的结果字典（含 _index 字段）。
        """
        if not tokens:
            return
        payload: dict[str, Any] = {"tokens": tokens}
        if proxy:
            payload["proxy"] = proxy
        try:
            resp = requests.post(
                f"{self.base_url}/api/extract-batch",
                json=payload,
                stream=True,
                timeout=(10, BATCH_LINE_TIMEOUT),
            )
            if resp.status_code != 200:
                err_result = _normalize_gateway_result({
                    "ok": False,
                    "code": f"http_{resp.status_code}",
                    "message": resp.text[:300],
                })
                for i in range(len(tokens)):
                    yield {**err_result, "_index": i}
                return
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                idx = int(raw.get("index", -1))
                result = _normalize_gateway_result(raw.get("result", raw))
                result["_index"] = idx
                yield result
        except requests.Timeout:
            err = _normalize_gateway_result({"ok": False, "code": "gateway_timeout", "message": "批量请求超时"})
            for i in range(len(tokens)):
                yield {**err, "_index": i}
        except requests.ConnectionError:
            err = _normalize_gateway_result({"ok": False, "code": "gateway_unreachable", "message": "无法连接 Go 网关"})
            for i in range(len(tokens)):
                yield {**err, "_index": i}
        except Exception as exc:
            err = _normalize_gateway_result({"ok": False, "code": "gateway_error", "message": str(exc)[:300]})
            for i in range(len(tokens)):
                yield {**err, "_index": i}

    def test_proxy(self, proxy: str) -> dict[str, Any]:
        """POST /api/test-proxy — 测试代理连通性。"""
        try:
            resp = requests.post(
                f"{self.base_url}/api/test-proxy",
                json={"proxy": proxy},
                timeout=15,
            )
            return resp.json() if resp.status_code == 200 else {"ok": False, "error": resp.text[:300]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------

_global_gateway: GptPpGateway | None = None


def get_gateway(**kwargs) -> GptPpGateway:
    """获取或创建全局网关实例。"""
    global _global_gateway
    if _global_gateway is None:
        _global_gateway = GptPpGateway(**kwargs)
    return _global_gateway


def shutdown_gateway() -> None:
    """关闭全局网关实例。"""
    global _global_gateway
    if _global_gateway is not None:
        _global_gateway.stop()
        _global_gateway = None


def generate_gateway_paypal_link(
    access_token: str,
    *,
    proxy: str = "",
    gateway_addr: str = DEFAULT_ADDR,
    auto_start: bool = True,
    timeout: int = 60,
    **kwargs,
) -> dict[str, Any]:
    """通过 Go 网关提取 PayPal 授权链接（单次）。

    与 gpt_pp_core.generate_gpt_pp_paypal_link() 接口对齐。
    """
    gw = GptPpGateway(addr=gateway_addr, auto_start=auto_start, timeout=timeout)
    if not gw.ensure_running():
        return {
            "ok": False,
            "error": "Go 网关未就绪",
            "error_code": "gateway_not_ready",
            "source": "gpt_pp_gateway",
            "link_type": "gpt_pp_paypal_authorize",
            "payment_method": "paypal",
            "terminal": False,
            "retryable": True,
        }
    try:
        return gw.extract(access_token, proxy=proxy)
    finally:
        if auto_start:
            gw.stop()


def generate_gateway_batch(
    tokens: list[str],
    *,
    proxy: str = "",
    gateway_addr: str = DEFAULT_ADDR,
    auto_start: bool = True,
) -> Generator[dict[str, Any], None, None]:
    """通过 Go 网关批量提取 PayPal 授权链接（NDJSON 流式）。"""
    gw = GptPpGateway(addr=gateway_addr, auto_start=auto_start)
    if not gw.ensure_running():
        err = {
            "ok": False,
            "error": "Go 网关未就绪",
            "error_code": "gateway_not_ready",
            "source": "gpt_pp_gateway",
        }
        for i in range(len(tokens)):
            yield {**err, "_index": i}
        return
    try:
        yield from gw.extract_batch(tokens, proxy=proxy)
    finally:
        if auto_start:
            gw.stop()
