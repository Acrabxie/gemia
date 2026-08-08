from types import SimpleNamespace

from gemia import file_browse_routes, session_manager


def test_session_file_root_uses_active_manager_output_root(tmp_path, monkeypatch):
    session_id = "v3-test-non-media"
    workdir = tmp_path / "workdirs" / session_id
    workdir.mkdir(parents=True)
    (workdir / "logo.svg").write_text("<svg/>", encoding="utf-8")

    monkeypatch.setattr(
        session_manager,
        "get_manager",
        lambda: SimpleNamespace(output_root=tmp_path),
    )

    assert file_browse_routes._resolve_root("session", session_id) == workdir


def test_frontend_routes_sandbox_svg_links_to_non_media_library():
    source = (
        file_browse_routes._REPO_ROOT / "static" / "v3" / "v3.js"
    ).read_text(encoding="utf-8")

    assert 'href.startsWith("sandbox:")' in source
    assert 'state.librarySection = "non-media"' in source
    assert "fetchSessionNonMediaAssets" in source
