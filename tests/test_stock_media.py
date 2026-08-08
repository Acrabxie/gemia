from __future__ import annotations

import json
from pathlib import Path

from gemia.ai.primitive_specs import primitive_spec_for_fqn
from gemia.video import stock_media


def test_search_stock_media_parses_pexels_video(monkeypatch) -> None:
    monkeypatch.setattr(stock_media, "_api_key", lambda provider: f"{provider}-key")

    def fake_json(url, *, headers=None):
        assert "videos/search" in url
        assert headers == {"Authorization": "pexels-key"}
        return {
            "videos": [
                {
                    "id": 123,
                    "width": 1920,
                    "height": 1080,
                    "duration": 8,
                    "url": "https://www.pexels.com/video/demo",
                    "image": "https://images.pexels.com/demo.jpg",
                    "user": {"name": "Creator"},
                    "video_files": [
                        {"width": 640, "height": 360, "link": "https://videos.pexels.com/sd.mp4"},
                        {"width": 1920, "height": 1080, "link": "https://videos.pexels.com/hd.mp4"},
                    ],
                }
            ]
        }

    monkeypatch.setattr(stock_media, "_urlopen_json", fake_json)

    result = stock_media.search_stock_media(query="city night", provider="pexels", media_type="video")

    item = result["results"][0]
    assert item["provider"] == "pexels"
    assert item["download_url"] == "https://videos.pexels.com/hd.mp4"
    assert item["width"] == 1920
    assert item["attribution"] == "Creator"


def test_stock_video_prefers_delivery_sized_hd_over_4k() -> None:
    selected = stock_media._largest_media_file(  # noqa: SLF001 - selection contract
        [
            {"width": 3840, "height": 2160, "link": "4k"},
            {"width": 1920, "height": 1080, "link": "1080p"},
            {"width": 1280, "height": 720, "link": "720p"},
        ]
    )
    assert selected["link"] == "1080p"


def test_search_stock_media_parses_pixabay_image(monkeypatch) -> None:
    monkeypatch.setattr(stock_media, "_api_key", lambda provider: f"{provider}-key")

    def fake_json(url, *, headers=None):
        assert "pixabay-key" in url
        assert "image_type=photo" in url
        return {
            "hits": [
                {
                    "id": 456,
                    "tags": "mountain lake",
                    "imageWidth": 3000,
                    "imageHeight": 2000,
                    "largeImageURL": "https://pixabay.com/photo.jpg",
                    "previewURL": "https://pixabay.com/preview.jpg",
                    "pageURL": "https://pixabay.com/photos/demo",
                    "user": "Artist",
                }
            ]
        }

    monkeypatch.setattr(stock_media, "_urlopen_json", fake_json)

    result = stock_media.search_stock_media(query="mountain", provider="pixabay", media_type="image")

    item = result["results"][0]
    assert item["provider"] == "pixabay"
    assert item["media_type"] == "image"
    assert item["download_url"] == "https://pixabay.com/photo.jpg"
    assert item["license"] == "Pixabay Content License"


def test_fetch_stock_media_downloads_and_writes_sidecar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        stock_media,
        "search_stock_media",
        lambda **_kwargs: {
            "query": "city",
            "provider": "auto",
            "media_type": "video",
            "results": [
                {
                    "provider": "pexels",
                    "id": "abc",
                    "media_type": "video",
                    "width": 1280,
                    "height": 720,
                    "duration": 5,
                    "download_url": "https://example.test/clip.mp4",
                }
            ],
            "errors": [],
        },
    )

    class FakeResponse:
        headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"fake mp4"

    monkeypatch.setattr(stock_media.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    output = stock_media.fetch_stock_media(
        "/tmp/input.mp4",
        str(tmp_path / "out.mp4"),
        query="city",
        import_to_media_library=False,
    )

    path = Path(output)
    assert path.read_bytes() == b"fake mp4"
    sidecar = path.with_suffix(path.suffix + ".stock.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["source"]["provider"] == "pexels"
    assert payload["imported_asset_id"] is None


def test_fetch_stock_media_selects_exact_prior_result_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        stock_media,
        "search_stock_media",
        lambda **_kwargs: {
            "query": "city",
            "provider": "pexels",
            "media_type": "video",
            "results": [
                {
                    "provider": "pexels",
                    "id": "first",
                    "media_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": 8,
                    "download_url": "https://example.test/first.mp4",
                },
                {
                    "provider": "pexels",
                    "id": "wanted",
                    "media_type": "video",
                    "width": 640,
                    "height": 360,
                    "duration": 4,
                    "download_url": "https://example.test/wanted.mp4",
                },
            ],
            "errors": [],
        },
    )

    class FakeResponse:
        headers = {"Content-Type": "video/mp4"}

        def __init__(self, url: str) -> None:
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.url.encode("utf-8")

    monkeypatch.setattr(
        stock_media.urllib.request,
        "urlopen",
        lambda request, **_kwargs: FakeResponse(request.full_url),
    )
    output = stock_media.fetch_stock_media(
        "",
        str(tmp_path / "selected.mp4"),
        query="city",
        result_id="wanted",
        import_to_media_library=False,
    )
    assert Path(output).read_bytes().endswith(b"wanted.mp4")


def test_stock_media_primitive_specs_are_tool_like() -> None:
    fetch = primitive_spec_for_fqn("gemia.video.stock_media.fetch_stock_media")
    search = primitive_spec_for_fqn("gemia.video.stock_media.search_stock_media")

    assert fetch["input_media"] == []
    assert fetch["output_media"] == "video|image"
    assert fetch["cost"] == "external_api"
    assert "external_download" in fetch["side_effects"]
    assert "query" in fetch["ask_if_missing"]
    assert search["output_media"] == "metadata"
