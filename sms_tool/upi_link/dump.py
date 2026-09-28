from __future__ import annotations

try:  # pragma: no cover - direct script execution
    from ..phone_proxy import redact_proxy_text
except ImportError:
    from phone_proxy import redact_proxy_text  # type: ignore
from typing import Any
from pathlib import Path
import json
import re
import threading
import time
from .constants import UPI_DUMP_DEFAULT_DIR
from .env import _env_bool, _env_int, _env_str


_dump_lock = threading.RLock()
_dump_counter = 0


def _upi_redact_for_dump(text: Any) -> str:
    """把一段文本里的凭据抹掉，供落盘/日志使用。

    四道过滤，缺一不可（参考实现只做了前三道；代理凭据是我们这边多出来的，
    因为本项目的代理由调用方传入，很容易带在请求体或错误文本里）：
      1. ``Authorization: Bearer <token>``
      2. ``__Secure-next-auth.session-token=<token>`` 及其它常见 cookie 名
      3. JSON / form 里的 ``access_token`` / ``sessionToken`` / ``token`` 字段
      4. 内联代理凭据 ``scheme://user:pass@host``（走项目 canonical 脱敏器）
    """
    value = str(text if text is not None else "")
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", value)
    value = re.sub(r"(?i)(__Secure-next-auth\.session-token=)[^;,\s\"']+", r"\1***", value)
    value = re.sub(
        r"(?i)((?:access[_-]?token|session[_-]?token|api[_-]?key|token)"
        r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{6,}",
        r"\1***",
        value,
    )
    try:
        value = redact_proxy_text(value)
    except Exception:  # pragma: no cover - redactor must never break dumping
        pass
    return value


def _upi_dump_dir() -> Path:
    """解析 dump 落盘目录。

    优先 ``UPI_DUMP_DIR``；否则相对**仓库根**取 ``runtime/upi_dumps``。
    仓库根用本文件位置推断（``sms_tool/upi_link/__init__.py`` 往上两级），
    不依赖 cwd——CLI 与 GUI 的 cwd 不一样。
    """
    configured = _env_str("UPI_DUMP_DIR", "")
    if configured:
        return Path(configured).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / UPI_DUMP_DEFAULT_DIR


def _upi_dump_http(
    response: Any,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> str:
    """把一次请求/响应落盘，返回写入的文件路径（未写则返回空串）。

    **绝不抛异常**：dump 是诊断辅助，不能因为它自己的问题（磁盘满、
    权限不足、响应对象没有 .text）把主流程带崩。所有失败都吞掉并返回 ""。

    触发条件：``force=True`` 或 ``UPI_DUMP=1``。
    调用方对 >=400 的响应传 ``force=True``——**失败现场永远要留证据**，
    哪怕总开关没开。
    """
    if not force and not _env_bool("UPI_DUMP", False):
        return ""
    global _dump_counter
    try:
        limit = _env_int("UPI_DUMP_LIMIT", 6000, minimum=500)
        with _dump_lock:
            _dump_counter += 1
            index = _dump_counter
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage or "stage"))
        directory = _upi_dump_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # 🔴 必须先 % 成 str 再 path join。``Path / "a_%04d_b.txt" % (1,)`` 会
        # 让 ``%`` 作用在 **Path 对象**上（``Path.__mod__`` 不存在）⇒ TypeError，
        # 而它被本函数的 ``except Exception`` 吞成 ""，表现为「开关开了也不落盘」。
        filename = "%s_%04d_%s.txt" % (stamp, index, safe_stage)
        path = directory / filename

        status = getattr(response, "status_code", "")
        url = getattr(response, "url", "") or request_url
        try:
            body_text = response.text if response is not None else ""
        except Exception:
            body_text = "<unreadable response body>"
        if request_body is None:
            rendered_request = ""
        else:
            try:
                rendered_request = json.dumps(request_body, ensure_ascii=False, indent=2)
            except Exception:
                rendered_request = repr(request_body)

        lines = [
            "stage: %s" % safe_stage,
            "request: %s %s" % (request_method, request_url),
            "",
            "request_body:",
            _upi_redact_for_dump(rendered_request)[:limit],
            "",
        ]
        if response is not None:
            lines.extend(
                [
                    "status: %s" % status,
                    "url: %s" % url,
                    "",
                    "response:",
                    _upi_redact_for_dump(body_text)[:limit],
                    "",
                ]
            )
        else:
            lines.extend(["response: <none>", ""])

        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        return str(path)
    except Exception:
        return ""


def _approve_backoff(attempt: int, cap: float) -> float:
    """approve 重试退避：随尝试次数线性增长，被 cap 截断。

    cap=0 时完全不睡（测试用）。参考实现是 random.uniform(1, 2)，
    这里改成确定性递增，便于把「重试确实退避了」写进断言。

    返回值恒 >= 0：``time.sleep`` 收到负数会抛 ValueError，
    而 attempt 从 1 起算，正常路径不会为负——但 helper 是纯函数，
    不能依赖调用方保证，所以自己夹住。
    """
    if cap <= 0:
        return 0.0
    return max(0.0, min(0.5 * attempt, cap))
