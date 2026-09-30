"""``scripts/sensitive_field_scan.py`` 的行为契约。

为什么要有这个文件
------------------
这个闸门此前**零测试**，而它当时是红的 —— 而且是两种不同性质的错：

* 🔴 **红线错在正确的树上**：策略嵌入断言把项目路径写死成
  ``SmsWorkbench/SmsWorkbench.csproj``，但提交 ``aeaea75``（协议支付规划器下沉
  ``SmsWorkbench.Contracts``）已经把 ``SensitiveDataSanitizer.cs`` 和嵌入一起搬走了。
  闸门于是持续报一个不存在的缺陷。红灯久了就没人看，真泄漏也就跟着过闸。
* 🟡 **对产物误报**：``ARTIFACT_SECRET`` 把两类**不是值的东西**当成值 ——
  ``refresh_token=missing_refresh_token``（``account_relogin_guard`` 的
  ``last_shape`` 失败词表）和 ``totp_secret=totp_secret``（源码片段里的关键字参数
  自回显）。误报的处置方式是忽略闸门，等于没有闸门。

所以两个方向都钉：**该抓的抓得住**，**不该抓的不吵**。

本文件**不**覆盖已知的 JSON 漏检（``"access_token": "..."`` 形态匹配不上）——
那是结构性缺口，修它要改成按 ``sensitive_policy.json`` 键名解析产物，是设计变更，
不在本次范围内。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "sensitive_field_scan",
    ROOT / "scripts" / "sensitive_field_scan.py",
)
assert _spec is not None
assert _spec.loader is not None
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


class TestPolicyEmbedding:
    """策略必须嵌入到**编译 sanitizer 的那个**项目里。"""

    def test_real_tree_resolves_and_passes(self):
        assert scanner._policy_embedding_failures() == []

    def test_owner_is_the_project_that_compiles_the_sanitizer(self):
        # 这条断言把 ``aeaea75`` 的搬迁钉住：消费者和嵌入必须在同一个工程，
        # 否则 ``SensitiveDataSanitizer`` 会在运行时找不到自己的资源清单项。
        owner = next(
            path
            for path in sorted(ROOT.glob("SmsWorkbench*/*.csproj"))
            if (path.parent / "SensitiveDataSanitizer.cs").is_file()
        )
        assert owner.name == "SmsWorkbench.Contracts.csproj"

    def test_missing_embed_is_reported(self, tmp_path: Path):
        project_dir = tmp_path / "SmsWorkbench.Contracts"
        project_dir.mkdir()
        (project_dir / "SensitiveDataSanitizer.cs").write_text("// stub", encoding="utf-8")
        (project_dir / "SmsWorkbench.Contracts.csproj").write_text("<Project />", encoding="utf-8")

        failures = scanner._policy_embedding_failures(tmp_path)

        assert failures == ["SmsWorkbench.Contracts.csproj: shared sensitive policy is not embedded"]

    def test_embed_in_a_project_that_does_not_compile_the_sanitizer_is_rejected(self, tmp_path: Path):
        # 嵌入放错工程同样是缺陷：消费者读的是**自己**程序集的清单项。
        app_dir = tmp_path / "SmsWorkbench"
        app_dir.mkdir()
        (app_dir / "SmsWorkbench.csproj").write_text(
            '<EmbeddedResource Include="..\\sensitive_policy.json" LogicalName="SmsWorkbench.sensitive_policy.json" />',
            encoding="utf-8",
        )
        contracts_dir = tmp_path / "SmsWorkbench.Contracts"
        contracts_dir.mkdir()
        (contracts_dir / "SensitiveDataSanitizer.cs").write_text("// stub", encoding="utf-8")
        (contracts_dir / "SmsWorkbench.Contracts.csproj").write_text("<Project />", encoding="utf-8")

        failures = scanner._policy_embedding_failures(tmp_path)

        assert failures == ["SmsWorkbench.Contracts.csproj: shared sensitive policy is not embedded"]

    def test_no_sanitizer_anywhere_is_reported(self, tmp_path: Path):
        app_dir = tmp_path / "SmsWorkbench"
        app_dir.mkdir()
        (app_dir / "SmsWorkbench.csproj").write_text("<Project />", encoding="utf-8")

        failures = scanner._policy_embedding_failures(tmp_path)

        assert failures == ["SensitiveDataSanitizer.cs: not compiled by any desktop project"]


@pytest.mark.parametrize(
    ("line", "expected_key"),
    [
        # JSON — the class that was 100% missed before: the key alternation required
        # ``=``/``:`` immediately after the key, and JSON has a quote in between.
        ('"access_token": "eyJhbGciOiJIUzI1NiJ9.abc"', "access_token"),
        ('"ba_token":"BA-9F2K7QX"', "ba_token"),
        ('{"totp_secret": "JBSWY3DPEHPK3PXP", "x": 1}', "totp_secret"),
        ('"card_number": "4111111111111111"', "card_number"),
        ('"refresh_token":"1//0gAbCdEfGhIjKlMnOp"', "refresh_token"),
        ('  "accessToken": "eyJhbGciOiJIUzI1NiJ9.abc",', "accesstoken"),
        # key=value / key: value — previously covered, must not regress.
        ("access_token=eyJhbGciOiJIUzI1NiJ9.abc", "access_token"),
        ("ba_token=BA-9F2K7QX", "ba_token"),
        ("refresh_token=1//0gAbCdEfGhIjKlMnOp", "refresh_token"),
        ("totp_secret=JBSWY3DPEHPK3PXP", "totp_secret"),
        ("card_cvv=737", "card_cvv"),
        # Prefixed variants still leak.
        ("oauth_refresh_token=xyzzy123", "refresh_token"),
        ("refresh-token=abc123", "refresh_token"),
        ("ba_token: BA-9F2K7QX", "ba_token"),
        # A non-empty single-quoted value is a value.
        ("access_token='eyJhbGciOiJIUzI1NiJ9.abc'", "access_token"),
    ],
)
def test_real_credential_values_are_still_detected(line: str, expected_key: str):
    match = scanner.ARTIFACT_SECRET.search(line)
    assert match is not None, f"a real credential in {line!r} would pass the gate"
    # The group preserves the literal spelling (``refresh-token``), so normalize it.
    assert match.group("artifact_key").lower().replace("-", "_") == expected_key


@pytest.mark.parametrize(
    "line",
    [
        # ``account_relogin_guard.json`` 的 last_shape 失败词表。
        '{"last_shape":"oauth_refresh_token=missing_refresh_token|web_session=missing_session_cookie"}',
        '"last_shape":"oauth_refresh_token=missing_refresh_token"',
        'chatgpt_email_otp=existing_login_otp_validate:(status=403)"},"x+oai01@icloud.com":{',
        # 源码片段里的关键字参数自回显（retired ``_phases*.json``）。
        "totp_secret=totp_secret,",
        "        totp_secret=totp_secret,",
        # 属性链回显（日志里被引用的源码行）。
        '{"refresh_token": s.oauth_refresh_token}',
        'subprocess.run(["x"], stdout=s.oauth_refresh_token)',
        # JSON 布尔 / null 是配置标志，不是值。
        '"ba_token": false',
        '"refresh_token": true',
        '"refresh_token": null',
        # 既有占位符形态，防止本次改动把它们弄丢。
        "access_token=[REDACTED]",
        'access_token="[REDACTED]"',
        '{"access_token": "[REDACTED]"}',
        'refresh_token=""',
        "totp_secret=''",
        "ba_token=None",
        "card_number=null",
    ],
)
def test_placeholder_and_echo_shapes_do_not_raise_findings(line: str):
    assert scanner.ARTIFACT_SECRET.search(line) is None, (
        f"{line!r} is a diagnostic sentinel or a source echo, not a leaked value; "
        "flagging it trains readers to ignore the gate"
    )


class TestArtifactScope:
    """The gate's scope is shipped artifacts, not by-design credential state.

    ``scripts/batch_enable_2fa.py`` journals TOTP secrets to
    ``runtime/analysis/2fa_enroll_report_*.jsonl`` and documents that file as
    "the only place the secret exists -- treat it as the recovery source of
    truth"; ``docs/directory-map.md`` defines ``runtime/`` as "Never commit;
    summarize redacted state only". A credential gate over a directory that holds
    credentials by design can only ever be red, and a permanently-red gate is the
    failure mode this file exists to prevent.
    """

    def test_json_artifact_is_detected_through_the_cli(self, tmp_path: Path):
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "report.json").write_text('{"access_token": "eyJhbGciOiJIUzI1NiJ9.abc"}', encoding="utf-8")

        assert scanner.main(["--artifacts", str(payload)]) == 1

    def test_clean_json_artifact_passes(self, tmp_path: Path):
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "report.json").write_text(
            '{"access_token": "[REDACTED]", "ba_token": false, "n": 0}', encoding="utf-8"
        )

        assert scanner.main(["--artifacts", str(payload)]) == 0

    def test_relative_artifacts_argument_does_not_crash(self, tmp_path: Path, monkeypatch):
        # Regression: ``--artifacts`` used to keep the raw argument, so an absolute
        # ``ROOT`` vs a relative payload made ``Path.relative_to`` raise and the
        # whole path crash -- which is why nothing could call it.
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "report.json").write_text('{"totp_secret": "JBSWY3DPEHPK3PXP"}', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert scanner.main(["--artifacts", "payload"]) == 1

    @staticmethod
    def _fake_repo(tmp_path: Path) -> None:
        """Give the faked root the policy file ``main`` validates."""
        (tmp_path / "sensitive_policy.json").write_text(
            (ROOT / "sensitive_policy.json").read_text(encoding="utf-8"), encoding="utf-8"
        )

    def test_default_run_does_not_scan_runtime(self, tmp_path: Path, monkeypatch):
        dirty = tmp_path / "runtime"
        dirty.mkdir()
        (dirty / "2fa_enroll_report.jsonl").write_text('{"totp_secret": "JBSWY3DPEHPK3PXP"}', encoding="utf-8")
        self._fake_repo(tmp_path)
        monkeypatch.setattr(scanner, "ROOT", tmp_path)
        monkeypatch.setattr(scanner, "_policy_embedding_failures", lambda root=None: [])

        # Would be 1 if ``runtime/`` were still in the default scan set.
        assert scanner.main([]) == 0

    def test_explicit_runtime_audit_reports_the_real_values(self, tmp_path: Path, monkeypatch):
        dirty = tmp_path / "runtime"
        dirty.mkdir()
        (dirty / "2fa_enroll_report.jsonl").write_text('{"totp_secret": "JBSWY3DPEHPK3PXP"}', encoding="utf-8")
        self._fake_repo(tmp_path)
        monkeypatch.setattr(scanner, "ROOT", tmp_path)
        monkeypatch.setattr(scanner, "_policy_embedding_failures", lambda root=None: [])

        # The capability stays available; it is only out of the default set.
        assert scanner.main(["--artifacts", str(dirty)]) == 1
