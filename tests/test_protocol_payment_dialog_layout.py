from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_protocol_payment_proxy_pool_editor_keeps_text_above_horizontal_scrollbar():
    source = (ROOT / "SmsWorkbench" / "MainWindow.Payment.cs").read_text(encoding="utf-8-sig")
    start = source.index("TextBox CreateProxyPoolBox()")
    end = source.index("var checkoutProxyPoolBox", start)
    editor = source[start:end]

    assert "VerticalContentAlignment = VerticalAlignment.Top" in editor
    assert "HorizontalContentAlignment = HorizontalAlignment.Left" in editor
    assert "Padding = new Thickness(8, 6, 8, 6)" in editor
