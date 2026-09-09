"""Generate the standalone purge .bat (GBK + CRLF, per house rule).

The background purge started inside WorkBuddy dies with the session, so the boss
needs a double-clickable script that can resume the same job at any time.  The
purge script is idempotent: it re-lists what is left and only deletes what exists.

Never hand-edit the generated .bat -- change this generator and re-run it.
"""

from __future__ import annotations

import os
import stat

PROJECT = r"F:\epsoft\GPT-Register-Tool"
TARGET = r"F:\tmp\purge-browser-profiles-20260909"
OUT = os.path.join(PROJECT, "scripts", "purge_browser_profiles.bat")

BODY = f"""@echo off
REM ============================================================
REM  browser profile 残留清理（可重复运行 / 可中断续跑）
REM
REM  目标目录：{TARGET}
REM  这是从 runtime/browser_profiles/ 同盘重命名挪出来的待删树，
REM  项目目录本身已经是空的，这里只负责把磁盘空间真正回收掉。
REM
REM  为什么是单线程：实测 4 / 10 并发会把杀软实时扫描队列压爆，
REM  结果 errors=done（一个都没删掉）；单线程顺序删 100% 成功。
REM
REM  生成脚本：scripts/gen_purge_bat.py  （不要手改本文件）
REM ============================================================

cd /d {PROJECT}

if not exist "{TARGET}" (
  echo [done] 目标目录已不存在，无需清理。
  goto :end
)

echo [start] 开始清理 %DATE% %TIME%
.venv\\Scripts\\python.exe -u scripts\\_purge_browser_profiles.py "{TARGET}" 1 3

if exist "{TARGET}" (
  echo.
  echo [warn] 仍有残留目录，可能是被占用/杀软锁住。稍后重跑本脚本即可续删。
) else (
  echo.
  echo [done] 清理完成，目标目录已删除。
)

:end
echo.
pause
"""


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = BODY.replace("\n", "\r\n").encode("gbk", errors="replace")
    with open(OUT, "wb") as fh:
        fh.write(payload)
    os.chmod(OUT, stat.S_IWRITE | stat.S_IREAD)
    print(f"written: {OUT} ({len(payload)} bytes, gbk+crlf)")


if __name__ == "__main__":
    main()
