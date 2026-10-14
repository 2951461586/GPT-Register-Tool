@echo off
REM ============================================================
REM  browser profile 残留清理（可重复运行 / 可中断续跑）
REM
REM  目标目录：F:\tmp\purge-browser-profiles-20260909
REM  这是从 runtime/browser_profiles/ 同盘重命名挪出来的待删树，
REM  项目目录本身已经是空的，这里只负责把磁盘空间真正回收掉。
REM
REM  为什么是单线程：实测 4 / 10 并发会把杀软实时扫描队列压爆，
REM  结果 errors=done（一个都没删掉）；单线程顺序删 100% 成功。
REM
REM  生成脚本：scripts/gen_purge_bat.py  （不要手改本文件）
REM ============================================================

cd /d F:\epsoft\GPT-Register-Tool

if not exist "F:\tmp\purge-browser-profiles-20260909" (
  echo [done] 目标目录已不存在，无需清理。
  goto :end
)

echo [start] 开始清理 %DATE% %TIME%
.venv\Scripts\python.exe -u scripts\_purge_browser_profiles.py "F:\tmp\purge-browser-profiles-20260909" 1 3

if exist "F:\tmp\purge-browser-profiles-20260909" (
  echo.
  echo [warn] 仍有残留目录，可能是被占用/杀软锁住。稍后重跑本脚本即可续删。
) else (
  echo.
  echo [done] 清理完成，目标目录已删除。
)

:end
echo.
pause
