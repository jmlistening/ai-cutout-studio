# 工程设计说明

本文档补充 README 没有展开的模块边界、请求生命周期、失败处理、测试策略和设计取舍。所有描述以当前版本 `1.3.0` 的代码为准。

## 1. 设计目标与非目标

### 目标

1. **本地优先**：默认只监听 `127.0.0.1`，不依赖账号、数据库或云托管。
2. **结果可信**：输出真正的 RGBA 透明 PNG，保留 alpha 通道，不用生成式近似图。
3. **批量可用**：队列、进度、单张重试、ZIP 导出，失败不影响其他素材。
4. **可解释**：上游错误映射成可行动的中文提示，并保留 `request_id` / `task_id`。
5. **可验证**：核心路径有 pytest 覆盖，CI 在 Windows / Ubuntu 上跑多版本矩阵。

### 非目标

- 不做多用户 SaaS、账号体系、配额计费。
- 不承诺公网部署安全；如果要暴露到局域网/公网，需要额外的认证、TLS、限流和审计。
- 不做图像编辑（换背景、修图、生成式补全），只做背景移除与结果归一化。
- 不把上游 SDK 封装成“全家桶”；只实现本项目实际用到的四步链路。

## 2. 模块职责

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `app.py` | 路由、中间件、异常映射、线程池调度、输出文件读写 | 图像算法细节、上游协议解析 |
| `config.py` | `.env` 解析、环境变量优先级、Key 清洗与掩码、原子保存 | 网络请求、界面状态 |
| `image_utils.py` | 解码保护、EXIF、GIF 首帧、场景限缩、编码选择、alpha 裁剪 | 调用上游、写文件 |
| `mediakit_client.py` | 四步 HTTP 链路、超时、响应解析、错误分类 | 决定 HTTP 对外的状态码 |
| `static/app.js` | 队列状态机、上传、预览、对比滑块、下载 | 直接持有或展示完整 Key |
| `static/index.html` | 工作台语义结构与可访问性标记 | 业务状态 |
| `static/style.css` | 视觉系统、布局、响应式与 checkerboard | 交互逻辑 |

依赖方向是单向的：`app.py → image_utils / mediakit_client / config`；`mediakit_client → image_utils` 只使用 `PreparedImage` 数据类型，不反向依赖 `app.py`。

## 3. 请求生命周期

### 3.1 `POST /api/cutout`

```text
1. 中间件：Content-Length 粗检、Origin 校验、X-Cutout-Client 校验
2. 读取上传流：最多 MAX_INPUT_BYTES + 1 字节
3. 检查 settings.configured 与 scene 合法性
4. 线程池执行 prepare_upload(raw, scene)
   - 体积 / 像素 / 格式检查
   - EXIF 方向、GIF 首帧、场景限缩
   - 原样透传或重编码到 ≤ MAX_UPLOAD_BYTES
5. 异步执行 segment_image(prepared, scene, crop_subject, settings)
   - 申请上传地址
   - PUT 预签名地址
   - remove-image-background
   - 下载 result.image_url
6. 线程池执行 normalize_result_image(result.image_bytes, crop_subject)
   - 统一 RGBA
   - 需要时 alpha bbox 裁剪
   - 重新编码 PNG
7. 生成 uuid4().hex，写入 outputs/<id>.png
8. 返回 JSON 元数据（不返回 Key、不返回上游签名 URL）
```

**为什么 CPU 操作用线程池？** Pillow 的解码、缩放、编码是同步阻塞的；直接放在 `async def` 路由里会阻塞事件循环，导致同一进程的其他请求（包括静态页面）卡住。`run_in_threadpool` 把 CPU 密集段移出事件循环。

**为什么先读 `MAX_INPUT_BYTES + 1`？** 这样既能检测是否超限，又不会把超大文件完整读进内存；超限时立即返回 413。

### 3.2 `GET /files/{id}`

- 只接受 `^[0-9a-f]{32}$` 的 ID，避免路径穿越。
- 只读取 `OUTPUT_DIR` 下的文件，不跟随用户提供的路径。
- `download=1` 时生成 RFC 5987 的 `filename*`，并用 ASCII fallback 兼容旧客户端。
- 响应头 `Cache-Control: private, max-age=3600`。

### 3.3 `POST /api/zip`

- 先校验所有 ID，再统计总大小；任何一项不存在或超过 500 MB 都整体拒绝，避免“下载到一半才失败”。
- 打包时用 Pillow 读取每张成品 PNG 的实际宽高，按 `{width}x{height}/` 分文件夹；读不出尺寸或触发解压炸弹保护时归入 `未知尺寸/`。
- 文件名走同一套 `sanitize_filename`，并用 `casefold()` 在**同一分辨率文件夹内**做大小写不敏感去重。
- 使用 `ZIP_DEFLATED`；PNG 本身压缩率有限，但能减少元数据与重复文件名的开销。

### 3.4 `POST /api/settings`

1. `_clean_api_key` 去掉首尾空白、`Bearer ` 前缀和成对引号；
2. 用 `^\S{8,}$` 拒绝含空格或过短的 Key；
3. 写入同目录的 `.env.tmp`，再 `os.replace` 原子替换 `.env`；
4. 删除进程环境变量 `MEDIAKIT_API_KEY`，防止旧值继续覆盖新文件；
5. 立即 `reload()`，不重启服务。

## 4. 图像管线：决策表

`prepare_upload()` 的核心判断：

| 条件 | 结果 |
| --- | --- |
| 无 EXIF 旋转 + 未触发场景缩放 + 格式在 `RAW_FORMATS` + 原始字节 ≤ 28 MB | **原样发送**，`transcoded = false` |
| 有 EXIF 旋转 / 触发缩放 / 格式未知 / 原始字节 > 28 MB | 解码后重新编码，`transcoded = true` |
| 图片含 alpha 通道且不是完全不透明 | PNG 输出，保留透明 |
| 图片含 alpha 但 alpha 全为 255 | 转为 RGB JPEG，减小体积 |
| JPEG 编码后仍 > 28 MB | 先尝试 quality 88/84/80/76，再按 0.85 迭代缩小 |
| 缩放后尺寸不变且仍超限 | 抛出“图片压缩后仍然太大” |
| 解码前像素数 > 60 MP | 拒绝，避免解码炸弹 |

**边界：**

- `Image.MAX_IMAGE_PIXELS = 60_000_000`，与手动头部检查形成双重保护。
- GIF 只取第一帧；动图的其余帧不会上传，也不会出现在结果里。
- TIFF / HEIC 在 `RAW_FORMATS` 中，但前端上传入口不接受它们；这是“服务端能力”与“浏览器可预览性”的刻意区分。

## 5. 错误分类

`MediaKitError` 把错误分成两类信息：

1. **业务 code**：给程序判断，例如 `MEDIAKIT_AUTH`、`MEDIAKIT_BILLING`；
2. **用户消息**：给界面展示，包含上游原始 message 的前 160 个字符。

分类顺序很重要：先判断欠费/余额，再判断未开通/权限，再判断 Key 无效，最后才是限流和尺寸。原因是上游的 message 可能同时包含多个关键词，顺序决定最终提示。

`app.py` 的异常处理器再把业务 code 映射为对外 HTTP 状态，前端只根据 `response.ok` 和 `error.message` 展示，不依赖内部 code。

### 5.1 本地空白结果校验

上游 HTTP 200 不代表结果有效：模型可能返回一张全透明图片。`normalize_result_image()` 在统一转 RGBA 后检查 alpha 通道的包围盒：

```python
alpha_bbox = img.getchannel("A").getbbox()
if alpha_bbox is None:
    raise ImageInputError("抠图接口返回了空白结果，请换一张图片重试")
```

`app.py` 会把它映射成 502 `UPSTREAM_BAD_IMAGE`，并且**不会写入 `outputs/`**。对应的测试是 `test_normalize_result_rejects_blank_result`。

### 5.2 重试策略

本项目**不做自动重试**。原因是上游按成功处理次数计费，自动重试会在余额不足、参数错误或上游抖动时产生不可控的重复扣费。失败项由用户在界面上手动重试；每次重试都是一次新的上游调用。这个取舍在 README 的“已知边界”里明确写出。

## 6. 前端状态机

```text
                 ┌──────────────┐
                 │   pending    │
                 └──────┬───────┘
                        │ 点击开始 / 全部抠图
                        ▼
                 ┌──────────────┐
                 │  processing  │
                 └──┬───────┬───┘
             成功   │       │  失败
                    ▼       ▼
             ┌──────────┐ ┌────────┐
             │   done   │ │ error  │
             └──────────┘ └───┬────┘
                              │ 重新抠图
                              └──────► processing
```

- 批量模式是 `for ... of` 串行循环，不是 `Promise.all`；这是为了降低上游限流概率，并让进度条真实反映“第几张”。
- 单张失败只标记该项，不中断整个队列；`state.configured` 变成 false 时才停止批处理并打开凭证弹窗。
- `renderAll()` 是唯一的状态渲染入口；所有事件处理器只修改 `state`，再调用渲染，避免 DOM 状态与数据状态分叉。
- 键盘 `↑` / `↓` 按 `state.items` 顺序循环切换选中项；输入框、单选按钮和凭证弹窗保留原生方向键行为。
- 素材的本地预览使用 `URL.createObjectURL(file)`；删除素材、清空队列、页面卸载时都会 `revokeObjectURL`。

## 7. 安全清单

| 检查项 | 实现位置 | 测试 |
| --- | --- | --- |
| 只监听回环地址 | `app.py: main()` | 人工 / 部署检查 |
| Origin 白名单 | `_local_only_guard` | `test_cutout_rejects_cross_origin` |
| 自定义请求头 | `_local_only_guard` | `test_cutout_requires_client_header` |
| 上传体积粗检 | `_local_only_guard` | 代码审查 |
| 文件 ID 白名单 | `_output_path` | `test_file_route_blocks_path_traversal` |
| 文件名清洗 | `sanitize_filename` | `test_sanitize_filename_hardening` |
| ZIP 名称去重 | `_build_zip` | `test_zip_download_dedupes_names`、`test_zip_dedupes_names_within_each_resolution_folder` |
| Key 不回传 | `_status_payload` / `masked_key` | `test_status_without_credentials`、`test_settings_saved_and_key_masked` |
| Key 原子写入 | `Settings.save_api_key` | `test_save_api_key` |
| 解码炸弹 | `prepare_upload` 头部检查 | `test_rejects_too_many_pixels` |
| 安全响应头 | `_security_headers` | 代码审查 |

## 8. 测试策略

### 8.1 不依赖真实上游

`tests/test_mediakit.py` 用 `httpx.MockTransport` 替换 `_client()`，构造一个四步响应序列，并断言：

- 第一步带 `Authorization: Bearer ...`；
- 上传到对象存储时**不带** MediaKit 鉴权头，只带预签名 `Content-Type`；
- 第三步 body 的 `image_url` 是 `mediakit://file-123`；
- 第四步下载结果并返回 RGBA PNG；
- 请求顺序严格是：申请地址 → PUT → 抠图 → 下载。

这样 CI 不需要 API Key、不需要外网，也不会产生费用。

### 8.2 隔离真实配置

`tests/test_api.py` 的 autouse fixture 会把 `config.ENV_PATH` 指向 `tmp_path`，把 `OUTPUT_DIR` 指向临时目录，并删除 `MEDIAKIT_API_KEY` / `MEDIAKIT_ENDPOINT` 环境变量。测试不会读写开发者本机的 `.env`，也不会把结果写进项目 `outputs/`。

### 8.3 前端契约测试

`tests/test_frontend_static.py` 不启动浏览器，而是用正则检查：

- `app.js` 里 `getElementById("...")` 引用的每个 ID 都存在于 HTML；
- HTML 引用的 `/static/*` 文件都存在；
- 三种视图 `compare` / `result` / `source` 都存在；
- 素材队列的 `↑` / `↓` 切换逻辑与输入框/弹窗保护存在；
- CSS 对 `[hidden]` 有强制隐藏规则；
- 前端文件不含裸控制字符。

这不是端到端测试，但能在零依赖的情况下抓住最常见的“改了 ID 忘了同步”问题。

## 9. 资源与性能边界

| 项目 | 限制 | 位置 |
| --- | ---: | --- |
| 单张输入 | 30 MB | `image_utils.MAX_INPUT_BYTES` |
| 上传给上游 | 28 MB | `image_utils.MAX_UPLOAD_BYTES` |
| 解码像素 | 60 MP | `image_utils.MAX_DECODE_PIXELS` |
| 通用场景 | 长边 10240 / 短边 6000 / 40 MP | `SCENE_LIMITS` |
| 人像 / 商品 | 7680 × 4320 | `SCENE_LIMITS` |
| 上游结果 | 80 MB | `mediakit_client.MAX_RESULT_BYTES` |
| 队列 | 50 张 | `static/app.js MAX_FILES` |
| ZIP | 100 张 / 500 MB | `app.py api_zip` |
| 上游超时 | 180 秒（连接 20 秒） | `MEDIAKIT_TIMEOUT` |

**并发特征：** 单进程、单事件循环；图像 CPU 工作在线程池，上游请求异步；批量处理在前端串行。这是为“本地单用户”设计的，不是高并发服务。

**实测样本：** 2026-10-24 在 Windows 11 / Python 3.12.5 上用 3 张合成演示图各调用一次真实 MediaKit 接口，单张耗时约 3–5 秒（含申请上传地址、PUT、抠图、下载四步）。这只是冒烟观测，不是性能基准；延迟取决于网络、图片尺寸和上游排队情况。

## 10. 替代方案与取舍

### 10.1 官方 SDK vs 手写 httpx

**选择：手写。** 四步链路只有四个端点，手写能精确控制预签名头上传、`file_id` 前缀补全、三种 `upload_headers` 形态和错误分类；同时避免引入一个不透明的重依赖。代价是上游协议变更时需要自己跟进，测试里已经用 MockTransport 固化了当前协议。

### 10.2 数据库 vs 文件系统

**选择：文件系统。** 结果是一次性 PNG，ID 是 uuid4；没有查询、关联或迁移需求。文件系统让“清空队列不删除结果”“用户自己打开 outputs 文件夹”这类行为自然成立。代价是没有索引和过期清理，后续如果要做历史记录，需要补一层轻量索引。

### 10.3 React/Vue vs 原生 JS

**选择：原生。** 单页面、单队列、三种视图；零构建让启动路径更短，也避免 npm 供应链和打包配置。代价是组件复用和复杂状态管理要靠自律，所以代码里用单一 `state` 对象和 `renderAll()` 收敛渲染。

### 10.4 异步任务队列 vs 同步请求

**选择：同步。** 本地单用户，单张通常几秒；同步请求让错误直接返回、进度真实、部署零依赖。代价是超大图或上游慢时会占用一个请求；默认 180 秒超时是显式的边界。批量场景在 README 的 Roadmap 里明确列为未来方向。

### 10.5 本地裁剪 vs 上游裁剪

**选择：混合。** 人像/商品把 `need_crop_background` 传给上游；通用场景不传，改在本地按 alpha bbox 裁剪。这样既避免通用场景的参数校验问题，又保证三种场景的“只保留主体”语义一致。

### 10.6 云 API vs 本地模型

这是面试里最容易被追问的选型问题。下面是比较维度，不是本仓库的基准测试；实际效果取决于素材、模型版本和硬件：

| 方案 | 发丝 / 复杂边缘 | 离线 | 单张延迟 | 成本 | 图片是否出本机 | 部署复杂度 |
| --- | --- | --- | --- | --- | --- | --- |
| **火山引擎 AI MediaKit** | 强 | 否 | 约 3–5 秒（网络） | 按成功次数计费 | 是 | 低（API Key） |
| rembg / U2Net | 中 | 是 | CPU 秒级 / GPU 更快 | 0 | 否 | 中（模型与依赖） |
| BiRefNet 等新模型 | 强 | 是 | 建议 GPU | 0（硬件成本） | 否 | 高（GPU / 权重） |
| 浏览器端 ONNX | 中 | 是 | 取决于设备 | 0 | 否 | 中（模型体积 / 兼容性） |
| remove.bg 等 SaaS | 强 | 否 | 网络 | 按张计费 | 是 | 低 |

**选择 MediaKit 的真实理由：** 开发机没有可用 GPU；发丝和商品边缘的质量比纯 CPU 的 U2Net 更稳定；按次计费适合低频批量；不需要维护模型权重和推理环境。代价是图片必须出本机，且依赖网络与上游配额。

**可替换性：** `mediakit_client.segment_image()` 是适配器边界。如果以后换成本地模型，只需要保持 `PreparedImage` 输入和 `MediaKitResult` 输出，`app.py` 与前端不用改。这是当前分层最重要的收益之一。

## 11. 故障排查 Playbook

| 现象 | 优先检查 | 常见原因 |
| --- | --- | --- |
| 「API Key 无效或没有权限」 | Key 是否来自 AI MediaKit API Key 页面 | 复制了 ARK Key / AK-SK，或 Key 不完整 |
| 「服务或图像工具未开通」 | 控制台是否开通 AI MediaKit / 图像工具 | 新账号未开通，或 Key 所属项目无权限 |
| 「余额或免费额度不足」 | 费用中心、资源包 | 免费额度用完或未充值 |
| 「图片超出尺寸或体积限制」 | 原图长边/短边/像素 | 超过场景限制；先本地缩小 |
| 「请求太频繁」 | 是否并发批量 | 上游限流；等待后重试 |
| 端口被占用 | `PORT` 环境变量 | 工具会自动尝试 8765–8794 |
| 页面能开但接口 403 | 是否从 `127.0.0.1` 打开 | Origin 不是本机，或缺少 `X-Cutout-Client` |
| 结果保存失败 | `outputs/` 是否可写、磁盘是否已满 | 权限或磁盘空间 |
| 透明边没裁掉 | 输出范围是否选「只保留主体」 | 通用场景走本地 alpha bbox；半透明边缘可能保留 |

## 12. 未来工作

- 后台任务队列与断点续跑（面向大批量）。
- 本地结果索引、缩略图、按时间清理。
- 可插拔的上游适配器（保持 `/api/cutout` 契约不变）。
- Docker / 反向代理示例，并配套认证与限流说明。
- 前端可访问性与键盘操作补全（当前对比滑块与素材队列已支持方向键）。

任何新增能力都应先补测试，再更新 README 的“已实现”与“已知边界”，避免把规划写成现状。
