from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_roughcut_review_ui_has_batch_upload_progress_and_human_controls() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'id="upload-input" multiple' in html
    assert 'id="library-roughcut-btn"' in html
    assert 'id="roughcut-job-status"' in html
    assert 'apiFetch("/media-library/prepare"' in source
    assert "cleanup_suggestions" in source
    assert 'data-roughcut-review="accept"' in source
    assert 'data-roughcut-review="reject"' in source
    assert 'data-roughcut-review="correct"' in source
    assert "data-roughcut-seek" in source
    assert "data-panel-lib-roughcut" in source
    assert 'tags.includes("roughcut")' in source
    assert 'refreshPanel("library")' in source
    assert ".roughcut-review" in css
    assert ".roughcut-job-status" in css
