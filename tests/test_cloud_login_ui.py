from pathlib import Path


def test_cloud_login_ui_uses_device_flow_without_exposing_device_secret():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static/v3/index.html").read_text(encoding="utf-8")
    source = (root / "static/v3/v3-auth.js").read_text(encoding="utf-8")
    css = (root / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'id="auth-cloud-start"' in html
    assert 'id="auth-cloud-code"' in html
    assert "/auth/device/start" in source
    assert "/auth/device/token" in source
    assert 'modal.classList.toggle("auth-required"' in source
    assert ".auth-modal.auth-required [data-auth-close]" in css
    assert "device_code" not in html
