import gemia.onboarding as onboarding
import server


def test_desktop_server_can_start_before_provider_setup(monkeypatch) -> None:
    started = {}

    class FakeServer:
        daemon_threads = False

        def __init__(self, address, handler):
            started["address"] = address
            started["handler"] = handler

        def serve_forever(self):
            started["served"] = True

    monkeypatch.setattr(onboarding, "ensure_onboarded", lambda: False)
    monkeypatch.setattr(server, "_load_config_keys", lambda: None)
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server, "_server_urls", lambda host, port: [])

    server.main("127.0.0.1", 60991, allow_unconfigured=True)

    assert started["address"] == ("127.0.0.1", 60991)
    assert started["handler"] is server._Handler
    assert started["served"] is True


def test_headless_server_still_exits_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(onboarding, "ensure_onboarded", lambda: False)
    monkeypatch.setattr(
        server,
        "ThreadingHTTPServer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("server must not bind")),
    )

    server.main("127.0.0.1", 60992)


def test_startup_health_blender_probe_does_not_import_video_runtime(monkeypatch) -> None:
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)
    monkeypatch.setenv("LUMERI_BLENDER_PATH", "")
    monkeypatch.setenv("GEMIA_BLENDER_PATH", "")

    status = server._lightweight_blender_status()

    assert status["available"] in {True, False}
    assert "gemia.video.blender_link" not in server._lightweight_blender_status.__code__.co_names
