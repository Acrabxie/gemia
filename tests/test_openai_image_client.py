from __future__ import annotations

import asyncio
import base64
import json

from gemia.ai.openai_image_client import OpenAIImageClient, endpoint_from_chat_url


class _Response:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Opener:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.request = None

    def open(self, request, *, timeout):
        self.request = request
        assert timeout == 12.0
        return _Response(self.payload)


def test_subscription_chat_url_maps_to_private_image_endpoint() -> None:
    assert endpoint_from_chat_url(
        "http://127.0.0.1:7808/v1/chat/completions"
    ) == "http://127.0.0.1:7808/v1/images/generations"


def test_client_posts_prompt_and_reference_bytes_without_returning_base64() -> None:
    encoded = base64.b64encode(b"png").decode("ascii")
    opener = _Opener({
        "data": [{"b64_json": encoded, "model": "gpt-image-2"}],
    })
    client = OpenAIImageClient(
        endpoint="http://127.0.0.1:7808/v1/images/generations",
        timeout_sec=12,
    )
    client._opener = lambda: opener

    result = asyncio.run(client.generate_image(
        prompt="a blue glass planet",
        reference_images=[b"reference"],
        size="1536x1024",
        request_id="req_1",
    ))

    payload = json.loads(opener.request.data.decode("utf-8"))
    assert payload["model"] == "gpt-image-2"
    assert payload["prompt"] == "a blue glass planet"
    assert payload["size"] == "1536x1024"
    assert payload["input_images"][0].startswith("data:image/png;base64,")
    assert result["image_bytes"] == b"png"
    assert result["request_id"] == "req_1"
