from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def test_protocol_payment_proxy_pool_editor_keeps_text_above_horizontal_scrollbar():
    root = ET.parse(ROOT / "SmsWorkbench" / "ProtocolPaymentWindow.xaml").getroot()
    namespace = {"wpf": "http://schemas.microsoft.com/winfx/2006/xaml/presentation"}
    editors = [
        item for item in root.findall(".//wpf:TextBox", namespace)
        if "ProxyPool" in item.attrib.get("Text", "")
    ]

    assert len(editors) == 2
    assert all(item.attrib.get("VerticalContentAlignment") == "Top" for item in editors)
    assert all(item.attrib.get("HorizontalContentAlignment") == "Left" for item in editors)
    assert all(item.attrib.get("Padding") == "8,6" for item in editors)


def test_protocol_payment_window_is_command_bound_mvvm_surface():
    root = ET.parse(ROOT / "SmsWorkbench" / "ProtocolPaymentWindow.xaml").getroot()
    namespace = {"wpf": "http://schemas.microsoft.com/winfx/2006/xaml/presentation"}
    buttons = root.findall(".//wpf:Button", namespace)
    commands = {item.attrib.get("Command") for item in buttons if item.attrib.get("Command")}

    assert "{Binding RunCommand}" in commands
    assert "{Binding TestProxyCommand}" in commands
    assert "{Binding SaveProxyCommand}" in commands
    assert "{Binding CancelCommand}" in commands
    assert not (ROOT / "SmsWorkbench" / "MainWindow.Payment.cs").read_text(encoding="utf-8").__contains__("new Window")
