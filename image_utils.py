# coding: utf-8
"""图片校验与上传前处理。

AI MediaKit 智能抠图限制（官方文档）：
- 单张 ≤ 30 MB
- general：长边 ≤ 10240、短边 ≤ 6000、总像素 ≤ 40 MP
- human/product：长边 ≤ 7680、短边 ≤ 4320
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_INPUT_BYTES = 30 * 1024 * 1024
MAX_UPLOAD_BYTES = 28 * 1024 * 1024
MAX_DECODE_PIXELS = 60_000_000

SCENE_LIMITS: dict[str, tuple[int, int, int]] = {
    "general": (10240, 6000, 40_000_000),
    "human": (7680, 4320, 7680 * 4320),
    "product": (7680, 4320, 7680 * 4320),
}

RAW_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "HEIF": "image/heic",
    "HEIC": "image/heic",
}

Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS


class ImageInputError(ValueError):
    """图片不符合 MediaKit 接口要求，message 可以直接展示给用户。"""


@dataclass
class PreparedImage:
    data: bytes
    mime: str
    width: int
    height: int
    original_width: int
    original_height: int
    resized: bool
    transcoded: bool
    size: int


def _encode(img: Image.Image, has_alpha: bool, quality: int = 92) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if has_alpha:
        try:
            img.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "image/png"
        except OSError:
            buffer = io.BytesIO()
            try:
                img.save(buffer, format="PNG")
                return buffer.getvalue(), "image/png"
            except OSError as exc:
                raise ImageInputError("图片编码失败，请换一张图片试试") from exc

    # 大尺寸、高熵图片在 libjpeg 开启 optimize 时可能报 "Suspension not allowed here"
    try:
        img.save(buffer, format="JPEG", quality=quality, subsampling=1, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except OSError:
        buffer = io.BytesIO()
        try:
            img.save(buffer, format="JPEG", quality=quality, subsampling=1)
            return buffer.getvalue(), "image/jpeg"
        except OSError as exc:
            raise ImageInputError("图片编码失败，请换一张图片试试") from exc


def _scale(img: Image.Image, factor: float) -> Image.Image:
    width = max(1, int(round(img.width * factor)))
    height = max(1, int(round(img.height * factor)))
    if width == img.width and height == img.height:
        return img
    return img.resize((width, height), Image.LANCZOS)


def _fit_scene(img: Image.Image, scene: str) -> tuple[Image.Image, bool]:
    max_long, max_short, max_pixels = SCENE_LIMITS.get(scene, SCENE_LIMITS["general"])
    long_side, short_side = max(img.size), min(img.size)
    factor = 1.0
    if long_side > max_long:
        factor = min(factor, max_long / long_side)
    if short_side > max_short:
        factor = min(factor, max_short / short_side)
    pixels = img.width * img.height
    if pixels > max_pixels:
        factor = min(factor, math.sqrt(max_pixels / pixels))

    if factor >= 1.0:
        return img, False
    return _scale(img, factor), True


def prepare_upload(raw: bytes, scene: str = "general") -> PreparedImage:
    if not raw:
        raise ImageInputError("这个文件是空的")
    if len(raw) > MAX_INPUT_BYTES:
        raise ImageInputError("单张图片不能超过 30 MB，请压缩后再试")

    try:
        img = Image.open(io.BytesIO(raw))
        # 先看头部尺寸再解码，避免超大图在 load() 时占满内存
        if img.width * img.height > MAX_DECODE_PIXELS:
            raise ImageInputError("图片像素太大（超过 6000 万像素），请先缩小再试")
        img.load()
    except ImageInputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageInputError("无法识别这个文件，请上传 JPG、PNG、BMP、WebP 或 GIF 图片")
    except Image.DecompressionBombError:
        raise ImageInputError("图片像素太大，请先缩小再试")

    if getattr(img, "is_animated", False):
        img.seek(0)

    try:
        orientation = int(img.getexif().get(274, 1) or 1)
    except (AttributeError, TypeError, ValueError):
        orientation = 1

    oriented = orientation not in (1, 0)
    if oriented:
        img = ImageOps.exif_transpose(img)

    original_width, original_height = img.size
    if original_width < 1 or original_height < 1:
        raise ImageInputError("图片尺寸无效")

    fmt = (img.format or "").upper()
    fitted, scene_resized = _fit_scene(img, scene)
    if fitted is not img:
        img = fitted

    raw_ok = (
        not oriented
        and not scene_resized
        and fmt in RAW_FORMATS
        and len(raw) <= MAX_UPLOAD_BYTES
    )

    if raw_ok:
        return PreparedImage(
            data=raw,
            mime=RAW_FORMATS[fmt],
            width=img.width,
            height=img.height,
            original_width=original_width,
            original_height=original_height,
            resized=False,
            transcoded=False,
            size=len(raw),
        )

    has_alpha = (
        img.mode in ("RGBA", "LA")
        or (img.mode == "P" and "transparency" in img.info)
    )
    img = img.convert("RGBA" if has_alpha else "RGB")
    if has_alpha and img.getchannel("A").getextrema() == (255, 255):
        # 有 alpha 通道但完全不透明，按 JPEG 发送更省体积
        has_alpha = False
        img = img.convert("RGB")

    data, mime = _encode(img, has_alpha)
    used_qualities = False
    while len(data) > MAX_UPLOAD_BYTES:
        if not has_alpha and not used_qualities:
            used_qualities = True
            for quality in (88, 84, 80, 76):
                data, mime = _encode(img, has_alpha, quality=quality)
                if len(data) <= MAX_UPLOAD_BYTES:
                    break
            if len(data) <= MAX_UPLOAD_BYTES:
                break

        next_img = _scale(img, 0.85)
        if next_img.width == img.width and next_img.height == img.height:
            raise ImageInputError("图片压缩后仍然太大，请先缩小图片再试")
        img = next_img
        data, mime = _encode(img, has_alpha)

    return PreparedImage(
        data=data,
        mime=mime,
        width=img.width,
        height=img.height,
        original_width=original_width,
        original_height=original_height,
        resized=scene_resized or img.size != (original_width, original_height),
        transcoded=True,
        size=len(data),
    )


def normalize_result_image(
    raw: bytes, crop_subject: bool = False
) -> tuple[bytes, int, int]:
    """统一转成 RGBA PNG；需要时裁掉透明边。"""
    if not raw:
        raise ImageInputError("抠图接口返回了空图片")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ImageInputError("抠图接口返回的图片无法解析")

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # 全透明结果通常意味着上游识别失败；不要把它当成成功结果写进 outputs
    alpha_bbox = img.getchannel("A").getbbox()
    if alpha_bbox is None:
        raise ImageInputError("抠图接口返回了空白结果，请换一张图片重试")

    if crop_subject:
        img = img.crop(alpha_bbox)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), img.width, img.height
