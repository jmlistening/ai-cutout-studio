# coding: utf-8

from __future__ import annotations

import io
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_module
import config
from mediakit_client import MediaKitError, MediaKitResult

CUTOUT_HEADERS = {"X-Cutout-Client": "1"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path / "outputs")
    app_module.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("MEDIAKIT_API_KEY", raising=False)
    monkeypatch.delenv("MEDIAKIT_ENDPOINT", raising=False)

    settings = config.settings
    settings.api_key = ""
    settings.endpoint = config.DEFAULT_ENDPOINT
    yield
    settings.api_key = ""
    settings.endpoint = config.DEFAULT_ENDPOINT


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture
def configured():
    settings = config.settings
    settings.api_key = "mk-test-123456"
    return settings


@pytest.fixture
def fake_cutout(monkeypatch):
    calls: dict = {}

    async def fake(prepared, *, scene="general", crop_subject=False, settings=None):
        calls.update(
            scene=scene,
            crop_subject=crop_subject,
            size=prepared.size,
            sent_width=prepared.width,
            sent_height=prepared.height,
        )
        image = Image.new("RGBA", (180, 120), (0, 0, 0, 0))
        for x in range(30, 150):
            for y in range(20, 100):
                image.putpixel((x, y), (200, 60, 40, 255))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return MediaKitResult(
            image_bytes=buffer.getvalue(),
            request_id="req-test-1",
            task_id="task-test-1",
            elapsed=1.23,
            width=180,
            height=120,
            image_format="png",
        )

    monkeypatch.setattr(app_module, "segment_image", fake)
    return calls


def image_bytes(size=(320, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 80, 60)).save(buffer, "PNG")
    return buffer.getvalue()


def post_cutout(client, **overrides):
    data = {
        "scene": "human",
        "output_mode": "original",
        "name": "我的 图",
    }
    data.update(overrides.pop("data", {}))
    headers = {**CUTOUT_HEADERS, **overrides.pop("headers", {})}
    files = overrides.pop(
        "files",
        {"file": ("原图.png", image_bytes(), "image/png")},
    )
    return client.post("/api/cutout", data=data, files=files, headers=headers, **overrides)


def test_index_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "AI MediaKit" in response.text
    assert "抠图台" in response.text


def test_status_without_credentials(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["masked_key"] == ""
    assert body["provider"] == "mediakit"
    assert body["limits"]["max_input_mb"] == 30
    assert "mk-test" not in response.text


def test_cutout_requires_client_header(client, configured):
    response = client.post(
        "/api/cutout",
        data={"scene": "general", "output_mode": "original"},
        files={"file": ("a.png", image_bytes(), "image/png")},
    )
    assert response.status_code == 403


def test_cutout_rejects_cross_origin(client, configured):
    response = post_cutout(client, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_cutout_not_configured(client):
    response = post_cutout(client)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "NOT_CONFIGURED"


def test_cutout_rejects_unknown_scene(client, configured):
    response = post_cutout(client, data={"scene": "landscape"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_SCENE"


def test_cutout_success_and_download(client, configured, fake_cutout):
    response = post_cutout(client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["mode"] == "original"
    assert body["scene"] == "human"
    assert body["width"] == 180 and body["height"] == 120
    assert body["name"] == "我的 图"
    assert body["request_id"] == "req-test-1"
    assert body["task_id"] == "task-test-1"
    assert body["elapsed"] == 1.23
    assert body["size"] > 0
    assert body["input"]["width"] == 320
    assert body["input"]["height"] == 240
    assert body["input"]["resized"] is False
    assert body["input"]["transcoded"] is False
    assert fake_cutout["scene"] == "human"
    assert fake_cutout["crop_subject"] is False

    stored = app_module.OUTPUT_DIR / f"{body['id']}.png"
    assert stored.is_file()

    preview = client.get(body["url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    result = Image.open(io.BytesIO(preview.content))
    assert result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((90, 60))[3] == 255

    download = client.get(f"{body['url']}?download=1&name=我的图")
    assert download.status_code == 200
    disposition = download.headers["content-disposition"]
    assert "attachment" in disposition
    assert "filename*=UTF-8''" in disposition


def test_cutout_crop_mode_crops_alpha(client, configured, fake_cutout):
    response = post_cutout(client, data={"output_mode": "crop"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "crop"
    assert (body["width"], body["height"]) == (120, 80)
    assert fake_cutout["crop_subject"] is True


def test_cutout_rejects_bad_image(client, configured, fake_cutout):
    response = post_cutout(
        client,
        files={"file": ("bad.png", b"not an image", "image/png")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMAGE_INVALID"


def test_cutout_rejects_blank_upstream_result(client, configured, monkeypatch):
    async def blank(*args, **kwargs):
        image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return MediaKitResult(image_bytes=buffer.getvalue())

    monkeypatch.setattr(app_module, "segment_image", blank)
    response = post_cutout(client)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_BAD_IMAGE"
    assert not list(app_module.OUTPUT_DIR.glob("*.png"))


def test_mediakit_error_http_status_mapping(client, configured, monkeypatch):
    async def raise_auth(*args, **kwargs):
        raise MediaKitError("key bad", code="MEDIAKIT_AUTH", status_code=403)

    monkeypatch.setattr(app_module, "segment_image", raise_auth)
    assert post_cutout(client).status_code == 401

    async def raise_bad_image(*args, **kwargs):
        raise MediaKitError("bad image", code="MEDIAKIT_BAD_IMAGE", status_code=422)

    monkeypatch.setattr(app_module, "segment_image", raise_bad_image)
    assert post_cutout(client).status_code == 422

    async def raise_timeout(*args, **kwargs):
        raise MediaKitError("timeout", code="MEDIAKIT_TIMEOUT", status_code=504)

    monkeypatch.setattr(app_module, "segment_image", raise_timeout)
    assert post_cutout(client).status_code == 504

    async def raise_upstream(*args, **kwargs):
        raise MediaKitError("upstream", code="MEDIAKIT_UPSTREAM", status_code=502)

    monkeypatch.setattr(app_module, "segment_image", raise_upstream)
    assert post_cutout(client).status_code == 502


def test_settings_saved_and_key_masked(client, tmp_path):
    response = client.post(
        "/api/settings",
        json={"api_key": "mk-test-123456"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["masked_key"].startswith("mk-tes")
    assert "mk-test-123456" not in response.text

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MEDIAKIT_API_KEY=mk-test-123456" in env_text

    status = client.get("/api/status").json()
    assert status["configured"] is True


def test_settings_validation(client):
    response = client.post("/api/settings", json={"api_key": ""})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_zip_download_dedupes_names(client, configured, fake_cutout):
    body = post_cutout(client).json()
    response = client.post(
        "/api/zip",
        json={
            "items": [
                {"id": body["id"], "name": "同一个名字"},
                {"id": body["id"], "name": "同一个名字"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["同一个名字.png", "同一个名字 (2).png"]
        assert archive.read("同一个名字.png").startswith(b"\x89PNG")


def test_zip_rejects_invalid_ids(client, configured):
    response = client.post("/api/zip", json={"items": [{"id": "../secret", "name": "x"}]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"

    response = client.post(
        "/api/zip", json={"items": [{"id": "a" * 32, "name": "x"}]}
    )
    assert response.status_code == 404


def test_file_route_blocks_path_traversal(client, configured):
    assert client.get("/files/abcd").status_code == 400
    assert client.get("/files/..%2F..%2Fwin.ini").status_code in (400, 404)


def test_sanitize_filename_hardening():
    assert app_module.sanitize_filename("正常名字") == "正常名字"
    assert app_module.sanitize_filename("CON") == "_CON"
    assert app_module.sanitize_filename("CON.txt") == "_CON.txt"
    assert app_module.sanitize_filename(r"..\..\evil") == "evil"
    assert app_module.sanitize_filename("a‮b") == "ab"
    assert app_module.sanitize_filename("\x00...") == "cutout"
    assert app_module.sanitize_filename("") == "cutout"


def test_cutout_recreates_missing_outputs_dir(client, configured, fake_cutout):
    import shutil

    shutil.rmtree(app_module.OUTPUT_DIR)
    assert not app_module.OUTPUT_DIR.exists()

    response = post_cutout(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (app_module.OUTPUT_DIR / f"{body['id']}.png").is_file()
