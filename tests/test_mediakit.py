# coding: utf-8

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import mediakit_client
from image_utils import PreparedImage
from mediakit_client import MediaKitError, segment_image

ENDPOINT = "https://mediakit-test.example.com"


def _result_png() -> bytes:
    image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    for x in range(10, 50):
        for y in range(8, 32):
            image.putpixel((x, y), (30, 90, 70, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _prepared() -> PreparedImage:
    return PreparedImage(
        data=b"original-image-bytes",
        mime="image/png",
        width=100,
        height=80,
        original_width=100,
        original_height=80,
        resized=False,
        transcoded=False,
        size=20,
    )


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "api_key": "mk-test-key",
        "endpoint": ENDPOINT,
        "configured": True,
        **overrides,
    }
    return SimpleNamespace(**values)


def _mock_client(handler):
    def factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=30,
            follow_redirects=True,
        )

    return factory


def test_full_upload_and_cutout_flow(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == mediakit_client.UPLOAD_TARGET_PATH:
            assert request.headers["authorization"] == "Bearer mk-test-key"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "request_id": "req-upload",
                    "result": {
                        "file_id": "mediakit://file-123",
                        "method": "PUT",
                        "upload_url": "https://upload.example.com/put/1",
                        "upload_headers": [
                            {"key": "Content-Type", "value": "image/png"}
                        ],
                    },
                },
            )
        if request.url.host == "upload.example.com":
            assert request.method == "PUT"
            assert request.content == b"original-image-bytes"
            assert request.headers.get("content-type") == "image/png"
            assert "authorization" not in request.headers
            return httpx.Response(200)
        if request.url.path == mediakit_client.REMOVE_BG_PATH:
            body = json.loads(request.content)
            assert body["image_url"] == "mediakit://file-123"
            assert body["scene"] == "human"
            assert body["need_crop_background"] is True
            assert body["output_format"] == "png"
            assert request.headers["authorization"] == "Bearer mk-test-key"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "task_id": "task-bg-1",
                    "request_id": "req-bg-1",
                    "result": {
                        "image_url": "https://output.example.com/result.png?sig=1",
                        "image_size": 1234,
                        "image_format": "png",
                        "image_width": 60,
                        "image_height": 40,
                    },
                },
            )
        if request.url.host == "output.example.com":
            return httpx.Response(200, content=_result_png())
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))

    result = asyncio.run(
        segment_image(_prepared(), scene="human", crop_subject=True, settings=_settings())
    )

    assert result.image_bytes.startswith(b"\x89PNG")
    assert result.request_id == "req-bg-1"
    assert result.task_id == "task-bg-1"
    assert result.width == 60 and result.height == 40
    assert result.elapsed > 0

    assert [request.url.path for request in calls] == [
        mediakit_client.UPLOAD_TARGET_PATH,
        "/put/1",
        mediakit_client.REMOVE_BG_PATH,
        "/result.png",
    ]


def test_file_id_without_scheme_gets_prefix(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == mediakit_client.UPLOAD_TARGET_PATH:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "file_id": "file-xyz",
                        "method": "PUT",
                        "upload_url": "https://upload.example.com/put/2",
                        "upload_headers": {},
                    },
                },
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(204)
        if request.url.path == mediakit_client.REMOVE_BG_PATH:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "image_url": "https://output.example.com/result.png",
                        "image_width": 60,
                        "image_height": 40,
                        "image_format": "png",
                    },
                },
            )
        if request.url.host == "output.example.com":
            return httpx.Response(200, content=_result_png())
        raise AssertionError(request.url)

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    asyncio.run(segment_image(_prepared(), scene="general", settings=_settings()))
    assert captured["body"]["image_url"] == "mediakit://file-xyz"
    assert captured["body"]["scene"] == "general"
    assert "need_crop_background" not in captured["body"]


def test_invalid_api_key_is_friendly(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "error": {
                    "code": "AccessDenied",
                    "type": "Forbidden",
                    "message": "InvalidParameter.InvalidAPIKey:Get api key error",
                },
            },
        )

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    with pytest.raises(MediaKitError) as info:
        asyncio.run(segment_image(_prepared(), settings=_settings()))
    assert info.value.code == "MEDIAKIT_AUTH"
    assert "API Key" in info.value.message


def test_upstream_business_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == mediakit_client.UPLOAD_TARGET_PATH:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "file_id": "mediakit://f",
                        "method": "PUT",
                        "upload_url": "https://upload.example.com/put/3",
                        "upload_headers": [],
                    },
                },
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "success": False,
                "error": {"code": "InvalidParameter", "message": "image size too large"},
            },
        )

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    with pytest.raises(MediaKitError) as info:
        asyncio.run(segment_image(_prepared(), settings=_settings()))
    assert info.value.code == "MEDIAKIT_BAD_IMAGE"


def test_missing_credentials_fails_before_network(monkeypatch):
    def factory():
        raise AssertionError("不应发起网络请求")

    monkeypatch.setattr(mediakit_client, "_client", factory)
    with pytest.raises(MediaKitError) as info:
        asyncio.run(segment_image(_prepared(), settings=_settings(configured=False)))
    assert info.value.code == "NOT_CONFIGURED"


def test_invalid_scene_falls_back_to_general(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == mediakit_client.UPLOAD_TARGET_PATH:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "file_id": "mediakit://f",
                        "method": "PUT",
                        "upload_url": "https://upload.example.com/put/4",
                        "upload_headers": {},
                    },
                },
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(200)
        if request.url.path == mediakit_client.REMOVE_BG_PATH:
            captured["scene"] = json.loads(request.content)["scene"]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "image_url": "https://output.example.com/result.png",
                        "image_format": "png",
                    },
                },
            )
        return httpx.Response(200, content=_result_png())

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    asyncio.run(segment_image(_prepared(), scene="not-a-scene", settings=_settings()))
    assert captured["scene"] == "general"


def test_parse_upload_headers_variants():
    parse = mediakit_client._parse_upload_headers
    assert parse({"Content-Type": "image/png"}) == {"Content-Type": "image/png"}
    assert parse([{"key": "A", "value": "1"}, {"name": "B", "val": "2"}]) == {
        "A": "1",
        "B": "2",
    }
    assert parse([{"Key": "Content-Type", "Value": "image/png"}]) == {
        "Content-Type": "image/png"
    }
    assert parse(["X-Test: yes", "Broken"]) == {"X-Test": "yes"}


def test_billing_error_is_classified(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "success": False,
                "error": {"code": "InsufficientBalance", "message": "account balance is not enough"},
            },
        )

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    with pytest.raises(MediaKitError) as info:
        asyncio.run(segment_image(_prepared(), settings=_settings()))
    assert info.value.code == "MEDIAKIT_BILLING"
    assert "余额" in info.value.message


def test_service_not_activated_is_classified(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "success": False,
                "error": {"code": "Forbidden", "message": "service not activated for this account"},
            },
        )

    monkeypatch.setattr(mediakit_client, "_client", _mock_client(handler))
    with pytest.raises(MediaKitError) as info:
        asyncio.run(segment_image(_prepared(), settings=_settings()))
    assert info.value.code == "MEDIAKIT_PERMISSION"
    assert "未开通" in info.value.message
