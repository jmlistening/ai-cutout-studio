# coding: utf-8

from __future__ import annotations

import io

import pytest
from PIL import Image

import image_utils
from image_utils import (
    MAX_INPUT_BYTES,
    ImageInputError,
    normalize_result_image,
    prepare_upload,
)


def _image_bytes(
    size=(300, 200),
    mode="RGB",
    color=(120, 80, 60),
    fmt="PNG",
    **save_kwargs,
) -> bytes:
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, fmt, **save_kwargs)
    return buffer.getvalue()


def test_rejects_non_image():
    with pytest.raises(ImageInputError):
        prepare_upload(b"not an image")


def test_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(image_utils, "MAX_INPUT_BYTES", 100)
    with pytest.raises(ImageInputError):
        prepare_upload(b"x" * 101)


def test_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr(image_utils, "MAX_DECODE_PIXELS", 1000)
    with pytest.raises(ImageInputError):
        prepare_upload(_image_bytes((400, 300)))


def test_small_png_is_sent_as_is():
    raw = _image_bytes((300, 200))
    prepared = prepare_upload(raw)
    assert prepared.data == raw
    assert prepared.mime == "image/png"
    assert prepared.width == 300 and prepared.height == 200
    assert prepared.resized is False
    assert prepared.transcoded is False


def test_transparent_png_is_sent_as_is():
    raw = _image_bytes((300, 300), mode="RGBA", color=(0, 0, 0, 128))
    prepared = prepare_upload(raw)
    assert prepared.data == raw
    assert prepared.mime == "image/png"
    assert prepared.transcoded is False


def test_opaque_rgba_is_reencoded_as_jpeg_when_resizing():
    prepared = prepare_upload(
        _image_bytes((11000, 200), mode="RGBA", color=(10, 20, 30, 255)), "general"
    )
    assert prepared.mime == "image/jpeg"
    assert prepared.transcoded is True
    assert prepared.width <= 10240


def test_human_scene_resizes_to_7680():
    prepared = prepare_upload(_image_bytes((8000, 3000), color=(20, 80, 60)), "human")
    assert prepared.resized is True
    assert prepared.transcoded is True
    assert max(prepared.width, prepared.height) <= 7680
    assert min(prepared.width, prepared.height) <= 4320
    assert prepared.original_width == 8000
    assert prepared.original_height == 3000


def test_exif_orientation_is_applied():
    image = Image.new("RGB", (300, 200), (10, 20, 30))
    exif = image.getexif()
    exif[274] = 6  # 顺时针旋转 90 度
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)

    prepared = prepare_upload(buffer.getvalue())
    assert (prepared.original_width, prepared.original_height) == (200, 300)
    assert (prepared.width, prepared.height) == (200, 300)
    assert prepared.transcoded is True
    assert prepared.mime == "image/jpeg"


def test_gif_first_frame_is_transcoded():
    frames = [
        Image.new("RGB", (100, 80), (i * 60, 20, 20)) for i in range(2)
    ]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:], loop=0
    )
    prepared = prepare_upload(buffer.getvalue())
    assert prepared.transcoded is True
    assert (prepared.width, prepared.height) == (100, 80)
    assert prepared.mime in ("image/png", "image/jpeg")


def test_wide_image_is_fitted_for_general_scene():
    # 12000×2000 = 24MP，未超 40MP，但长边超过 10240
    prepared = prepare_upload(_image_bytes((12000, 2000), color=(30, 30, 30)), "general")
    assert prepared.resized is True
    assert prepared.width <= 10240


def test_normalize_result_can_crop_transparent_border():
    image = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
    for x in range(20, 60):
        for y in range(10, 40):
            image.putpixel((x, y), (200, 60, 40, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    png, width, height = normalize_result_image(buffer.getvalue(), crop_subject=True)
    assert (width, height) == (40, 30)
    assert Image.open(io.BytesIO(png)).mode == "RGBA"

    png_full, full_w, full_h = normalize_result_image(buffer.getvalue(), crop_subject=False)
    assert (full_w, full_h) == (100, 80)


def test_normalize_result_rejects_blank_result():
    image = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    with pytest.raises(ImageInputError):
        normalize_result_image(buffer.getvalue())


def test_normalize_result_rejects_garbage():
    with pytest.raises(ImageInputError):
        normalize_result_image(b"garbage")
