# coding: utf-8
"""火山引擎 AI MediaKit 图像背景移除（智能抠图）。

流程：
1. POST /api/v1/tools-sync/request-media-upload-url  申请上传地址
2. PUT 预签名地址，上传原始图片
3. POST /api/v1/tools-sync/remove-image-background   同步抠图
4. GET result.image_url 下载透明 PNG
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import Settings
from image_utils import PreparedImage

UPLOAD_TARGET_PATH = "/api/v1/tools-sync/request-media-upload-url"
REMOVE_BG_PATH = "/api/v1/tools-sync/remove-image-background"
TOOL_NAME = "remove-image-background"
VALID_SCENES = {"general", "human", "product"}
MAX_RESULT_BYTES = 80 * 1024 * 1024

logger = logging.getLogger("cutout.mediakit")


class MediaKitError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MEDIAKIT_ERROR",
        status_code: int | None = None,
        request_id: str = "",
        task_id: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.task_id = task_id


@dataclass
class MediaKitResult:
    image_bytes: bytes
    request_id: str = ""
    task_id: str = ""
    elapsed: float = 0.0
    width: int = 0
    height: int = 0
    image_format: str = "png"


def _auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _first(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _error_from_payload(payload: dict, status_code: int) -> MediaKitError:
    error = payload.get("error")
    code = ""
    message = ""
    if isinstance(error, dict):
        code = _first(error.get("code"))
        message = _first(error.get("message"))
    elif error is not None:
        message = str(error)

    request_id = _first(payload.get("request_id"))
    task_id = _first(payload.get("task_id"))
    combined = f"{code} {message}".lower()
    detail = message.strip()
    if len(detail) > 160:
        detail = detail[:160] + "…"

    logger.warning(
        "MediaKit error: status=%s code=%s request_id=%s message=%s",
        status_code, code or "-", request_id or "-", message[:300],
    )

    def with_detail(text: str) -> str:
        return f"{text}（{detail}）" if detail else text

    if any(
        key in combined
        for key in ("insufficient", "balance", "arrear", "欠费", "余额", "额度")
    ):
        return MediaKitError(
            with_detail("MediaKit 账户余额或免费额度不足，请在火山引擎费用中心充值或购买资源包"),
            code="MEDIAKIT_BILLING", status_code=402,
            request_id=request_id, task_id=task_id,
        )
    if any(
        key in combined
        for key in (
            "not activated", "not enabled", "not open", "service not",
            "permission denied", "未开通", "没有权限",
        )
    ):
        return MediaKitError(
            with_detail("AI MediaKit 服务或图像工具未开通，或当前 Key 没有权限"),
            code="MEDIAKIT_PERMISSION", status_code=403,
            request_id=request_id, task_id=task_id,
        )
    if (
        status_code in (401, 403)
        or "apikey" in combined
        or "api key" in combined
        or "invalidaccesskey" in combined
        or "accessdenied" in combined
    ):
        return MediaKitError(
            with_detail("MediaKit API Key 无效或没有权限，请在 AI MediaKit 控制台重新创建并填写"),
            code="MEDIAKIT_AUTH", status_code=status_code,
            request_id=request_id, task_id=task_id,
        )
    if status_code == 429:
        return MediaKitError(
            with_detail("请求太频繁了，稍等几秒再试"), code="MEDIAKIT_RATE_LIMIT",
            status_code=status_code, request_id=request_id, task_id=task_id,
        )
    if "size" in combined or "resolution" in combined or "too large" in combined or "分辨率" in message:
        return MediaKitError(
            with_detail("图片超出 MediaKit 的尺寸或体积限制，请换一张更小的图片"),
            code="MEDIAKIT_BAD_IMAGE", status_code=422,
            request_id=request_id, task_id=task_id,
        )

    detail_text = detail or f"HTTP {status_code}"
    return MediaKitError(
        f"抠图服务返回错误：{detail_text}",
        code=code or "MEDIAKIT_UPSTREAM",
        status_code=status_code,
        request_id=request_id,
        task_id=task_id,
    )


def _parse_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise MediaKitError(
            "MediaKit 返回了无法解析的内容，请稍后重试",
            code="MEDIAKIT_BAD_RESPONSE",
            status_code=response.status_code,
        )

    if not isinstance(payload, dict):
        raise MediaKitError(
            "MediaKit 返回格式异常",
            code="MEDIAKIT_BAD_RESPONSE",
            status_code=response.status_code,
        )
    if response.status_code >= 400 or payload.get("success") is False:
        raise _error_from_payload(payload, response.status_code)
    return payload


async def _post_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict
) -> dict:
    try:
        response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise MediaKitError(
            "请求 MediaKit 超时了，图片可能较大，请稍后重试",
            code="MEDIAKIT_TIMEOUT", status_code=504,
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaKitError(
            "连不上 MediaKit 接口，请检查网络或代理设置",
            code="MEDIAKIT_NETWORK", status_code=502,
        ) from exc
    return _parse_payload(response)


def _parse_upload_headers(value: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            if str(key).strip():
                headers[str(key).strip()] = str(val)
        return headers
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lowered = {str(k).lower(): v for k, v in item.items()}
                key = _first(lowered.get("key"), lowered.get("name"), lowered.get("header"))
                val = lowered.get("value", lowered.get("val", ""))
                if key:
                    headers[key] = str(val)
            elif isinstance(item, str) and ":" in item:
                key, val = item.split(":", 1)
                if key.strip():
                    headers[key.strip()] = val.strip()
    return headers


def _absolute_image_url(url: str, endpoint: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return endpoint + url
    return url


async def _request_upload_target(
    client: httpx.AsyncClient, settings: Settings
) -> tuple[str, str, str, dict[str, str]]:
    payload = await _post_json(
        client,
        settings.endpoint + UPLOAD_TARGET_PATH,
        _auth_headers(settings),
        {"tool_name": TOOL_NAME},
    )
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    file_id = _first(result.get("file_id"))
    upload_url = _first(result.get("upload_url"))
    method = (_first(result.get("method")) or "PUT").upper()
    headers = _parse_upload_headers(result.get("upload_headers"))

    if not file_id or not upload_url:
        raise MediaKitError(
            "MediaKit 没有返回上传地址，请稍后重试",
            code="MEDIAKIT_UPLOAD_TARGET",
            request_id=_first(payload.get("request_id")),
        )

    # 服务端返回的 file_id 通常已经是 mediakit://...；没有协议前缀时补上
    if not re.match(r"^[a-z][a-z0-9+.-]*://", file_id, re.IGNORECASE):
        file_id = "mediakit://" + file_id

    return file_id, upload_url, method, headers


async def _upload_bytes(
    client: httpx.AsyncClient,
    method: str,
    upload_url: str,
    headers: dict[str, str],
    data: bytes,
) -> None:
    try:
        response = await client.request(method, upload_url, headers=headers, content=data)
    except httpx.TimeoutException as exc:
        raise MediaKitError(
            "上传图片到 MediaKit 超时了，请稍后重试",
            code="MEDIAKIT_UPLOAD_TIMEOUT", status_code=504,
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaKitError(
            "上传图片到 MediaKit 失败，请检查网络后重试",
            code="MEDIAKIT_UPLOAD", status_code=502,
        ) from exc

    if response.status_code >= 400:
        raise MediaKitError(
            f"上传图片到 MediaKit 失败（HTTP {response.status_code}），请重试",
            code="MEDIAKIT_UPLOAD",
            status_code=response.status_code,
        )


async def _remove_background(
    client: httpx.AsyncClient,
    settings: Settings,
    file_id: str,
    scene: str,
    crop_subject: bool,
) -> dict:
    body = {
        "image_url": file_id,
        "scene": scene,
        "output_format": "png",
    }
    # 该参数只对 human/product 生效；general 直接不传，避免参数校验问题，
    # general 的「只保留主体」由本地 alpha bbox 裁剪兜底
    if scene in ("human", "product"):
        body["need_crop_background"] = bool(crop_subject)

    payload = await _post_json(
        client,
        settings.endpoint + REMOVE_BG_PATH,
        _auth_headers(settings),
        body,
    )
    result = payload.get("result") or {}
    if not isinstance(result, dict) or not _first(result.get("image_url")):
        raise MediaKitError(
            "MediaKit 没有返回抠图结果，请稍后重试",
            code="MEDIAKIT_NO_RESULT",
            request_id=_first(payload.get("request_id")),
            task_id=_first(payload.get("task_id")),
        )
    return payload


async def _download_result(
    client: httpx.AsyncClient, settings: Settings, image_url: str
) -> bytes:
    url = _absolute_image_url(image_url, settings.endpoint)
    try:
        response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise MediaKitError(
            "下载抠图结果超时了，请稍后重试",
            code="MEDIAKIT_DOWNLOAD_TIMEOUT", status_code=504,
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaKitError(
            "下载抠图结果失败，请检查网络后重试",
            code="MEDIAKIT_DOWNLOAD", status_code=502,
        ) from exc

    if response.status_code >= 400:
        raise MediaKitError(
            f"下载抠图结果失败（HTTP {response.status_code}），请稍后重试",
            code="MEDIAKIT_DOWNLOAD",
            status_code=response.status_code,
        )
    if not response.content:
        raise MediaKitError("抠图结果为空，请换一张图片试试", code="MEDIAKIT_EMPTY_RESULT")
    if len(response.content) > MAX_RESULT_BYTES:
        raise MediaKitError("抠图结果文件过大，请换一张更小的图片试试", code="MEDIAKIT_RESULT_TOO_LARGE")
    return response.content


def _client() -> httpx.AsyncClient:
    timeout = float(os.environ.get("MEDIAKIT_TIMEOUT", "180"))
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=20.0),
        follow_redirects=True,
    )


async def segment_image(
    prepared: PreparedImage,
    *,
    scene: str = "general",
    crop_subject: bool = False,
    settings: Settings,
) -> MediaKitResult:
    if not settings.configured:
        raise MediaKitError(
            "还没有配置 MediaKit API Key", code="NOT_CONFIGURED", status_code=400
        )
    if scene not in VALID_SCENES:
        scene = "general"

    started = time.perf_counter()
    async with _client() as client:
        file_id, upload_url, method, upload_headers = await _request_upload_target(
            client, settings
        )
        await _upload_bytes(client, method, upload_url, upload_headers, prepared.data)
        payload = await _remove_background(
            client, settings, file_id, scene, crop_subject
        )
        result = payload.get("result") or {}
        image_bytes = await _download_result(
            client, settings, _first(result.get("image_url"))
        )

    elapsed = time.perf_counter() - started
    return MediaKitResult(
        image_bytes=image_bytes,
        request_id=_first(payload.get("request_id")),
        task_id=_first(payload.get("task_id")),
        elapsed=elapsed,
        width=int(result.get("image_width") or 0),
        height=int(result.get("image_height") or 0),
        image_format=_first(result.get("image_format")) or "png",
    )
