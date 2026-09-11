"""`.gitignore` 与 pre-commit 守卫必须在「凭据型配置家族」上保持一致。

为什么值得单独一个文件
----------------------
2026-09-11 实测：`proxy.json.bak-vn-migration`（100 条代理账密 + 一个
smsbower api_key）以**未跟踪**状态躺在公开仓库根目录。两层防护同时失效：

1. `.gitignore` 只有精确的 `proxy.json`；既有的 `*.bak` / `*.bak_before_*` /
   `*.bak_*` 三条规则都挡不住**连字符**后缀 `.bak-vn-migration`。
2. `precommit_guard.BLOCKED_NAMES` 同样是精确名匹配，于是 `git add -f` 也拦不住。

更隐蔽的后果是 **release payload gate 失效**。`scripts/scan_release_payload.py`
那条"被 .gitignore 拒绝的文件绝不能出货"的规则是**问 git** 的
（`git check-ignore`）。git 说"没忽略"，闸门就放行 —— 护栏本身没坏，
是它的前提假设不成立。这类"门禁依赖另一个组件说真话"的结构最容易出假绿。

所以本文件断言的是**跨层一致性**：对同一批文件名，`.gitignore` 的判定必须
与守卫的判定完全相同。只测其中一层，两层就会悄悄漂移成"一个放行一个拦截"，
而真正决定出货与否的是 `.gitignore` 那一层。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "scripts"))
import precommit_guard  # noqa: E402


# 真源里带凭据的配置，以及它们的**任意快照**。后缀形态是不收敛的
# （`.bak` / `.bak-<描述>` / `.old` / `.save` / `.tmp`），所以按家族断言。
CREDENTIAL_CONFIG_SNAPSHOTS = (
    "proxy.json",
    "proxy.json.bak-vn-migration",   # 2026-09-11 事故原文
    "proxy.json.old",
    "proxy.json.save",
    "config.json",
    "config.json.bak",
    "runtime.json",
    "runtime.json.bak-20260911",
    "payment.json",
    "payment.json.tmp",
    "session.json",
)

# 只有占位符的示例模板，必须保持可入库。
ALLOWED_TEMPLATES = (
    "config.example.json",
    "proxy.json.example",
    "config.json.example",
)


def git_ignores(rel: str) -> bool:
    """真的去问 git，而不是解析 .gitignore 文本。

    解析文本会得到"我以为的规则"，而闸门用的是"git 实际的判定"——
    两者不一致正是本次事故的成因。

    用 ``check-ignore -v`` 并**解析匹配规则本身**，而不是只看退出码：
    git 2.21~2.22 的 check-ignore 对**例外规则**（``!proxy.json.example``）
    命中时也返回退出码 0（"被忽略"），2.23 起才修正；本机 git 2.21 正中该
    缺陷。但 -v 打印的"最后命中规则"在所有版本里语义一致：
    规则以 ``!`` 开头 = 未被忽略；无匹配 = 未被忽略。
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "--", rel],
        cwd=str(ROOT),
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"git check-ignore failed for {rel!r}: rc={proc.returncode} "
            f"stderr={proc.stderr.decode('utf-8', errors='replace').strip()!r}"
        )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        # 无任何规则命中 —— 未被忽略。
        return False
    # 行格式：<source>:<linenum>:<pattern>\t<pathname>。
    matched_pattern = out.splitlines()[0].split("\t", 1)[0].rsplit(":", 1)[-1]
    return not matched_pattern.startswith("!")


@pytest.mark.parametrize("rel", CREDENTIAL_CONFIG_SNAPSHOTS)
def test_gitignore_rejects_credential_config_snapshots(rel: str) -> None:
    """这一层才是决定性的：闸门只信 git。"""
    assert git_ignores(rel), (
        f"{rel} 未被 .gitignore 拒绝 —— release payload gate 对它完全失明"
        f"（闸门问的是 git，git 说没忽略就放行）"
    )


@pytest.mark.parametrize("rel", CREDENTIAL_CONFIG_SNAPSHOTS)
def test_precommit_guard_rejects_credential_config_snapshots(rel: str) -> None:
    assert precommit_guard.name_is_blocked(rel) is True, (
        f"{rel} 未被 pre-commit 守卫拦截 —— `git add -f` 可以绕过 .gitignore"
    )


@pytest.mark.parametrize("rel", ALLOWED_TEMPLATES)
def test_example_templates_stay_trackable(rel: str) -> None:
    """示例模板被吃掉是**静默**的：不入库、也不进载荷，且不报错。"""
    assert not git_ignores(rel), f"{rel} 被误忽略，将来无法入库"
    assert precommit_guard.name_is_blocked(rel) is False, f"{rel} 被守卫误拦"


@pytest.mark.parametrize("rel", CREDENTIAL_CONFIG_SNAPSHOTS + ALLOWED_TEMPLATES)
def test_the_two_layers_agree(rel: str) -> None:
    """跨层一致性 —— 本文件存在的主要理由。

    两层判定相反时没有报错，只有"某条路径在一个层里被放行"这个事实。
    断言相等，就让漂移变成一次失败，而不是一次泄漏。
    """
    assert git_ignores(rel) is precommit_guard.name_is_blocked(rel), (
        f"{rel}：.gitignore 判定={git_ignores(rel)}，"
        f"pre-commit 守卫判定={precommit_guard.name_is_blocked(rel)} —— 两层已漂移"
    )


# ------------------------------------------------------- release payload gate

GATE = ROOT / "scripts" / "scan_release_payload.py"


def run_payload_gate(payload: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(payload)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_payload_gate_blocks_an_unanticipated_suffix(tmp_path: Path) -> None:
    """回归：闸门曾经是 **fail-open** 的。

    它只把"source-ish 后缀白名单"里的文件送去问 git。`proxy.json.bak-vn-migration`
    的 suffix 是 `.bak-vn-migration`，不在白名单里 —— 于是即使 `.gitignore` 已经
    拒绝它，闸门照样放行（2026-09-11 变异验证 M3 存活才暴露出来）。
    这条测试用一个后缀不被任何白名单承认的 git-ignored 文件钉住 fail-closed 行为。
    """
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "proxy.json.bak-vn-migration").write_text("{}", encoding="utf-8")

    proc = run_payload_gate(payload)

    assert proc.returncode != 0, (
        "闸门放行了一个 git-ignored、后缀又不被任何白名单承认的文件 —— "
        "fail-open 又回来了\n" + proc.stdout + proc.stderr
    )
    assert "proxy.json.bak-vn-migration" in (proc.stdout + proc.stderr), (
        "闸门拦了，但报错里没点名是哪个文件\n" + proc.stdout + proc.stderr
    )


def test_payload_gate_passes_a_clean_payload(tmp_path: Path) -> None:
    """反向：闸门不能变成"一律拦截"—— 那样上面那条测试也会绿。"""
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "README.md").write_text("# clean\n", encoding="utf-8")

    proc = run_payload_gate(payload)

    assert proc.returncode == 0, (
        "干净的载荷被闸门拦了 —— 说明它现在是 fail-closed 过头（一律拦截）\n"
        + proc.stdout + proc.stderr
    )
