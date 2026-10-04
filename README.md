# 🛰️ 航拍农田报告工作台

Web 地图 + 时间线联动的航拍巡检工作台：地块版本化、发现项（多轮巡检证据）、视频/图片/语音片段；移动端断网录入、分块续传、跨设备补证据；服务端用**关系库（SQLite）+ 内容寻址对象存储（本地目录，可平滑替换为 S3）**维护上传、空间版本与报告。

零第三方 Python 依赖（仅标准库）；前端 Leaflet 已 vendored 到 `static/vendor`，运行时不依赖外网（地图瓦片除外）。

## 运行

```bash
python3 server/server.py --port 8000
# 另开终端灌入演示数据（需服务已启动）：
python3 server/seed.py
# 自动化验收（19 条，可重复运行）：
python3 server/test_acceptance.py
```
打开 http://localhost:8000 ，右上角可切换 王农技 / 李巡检 / 张站长。手机视图用“📱 现场录入”标签（窄屏自动出现）。

## 目录
```
server/db.py      关系库 schema + 对象/分块/导出目录
server/geo.py     点在多边形、相机帧→地面严格投影、误差预算
server/app.py     业务逻辑：上传会话、去重、发现/多次巡检、冲突、重判、报告、导出、转写队列
server/server.py  纯 stdlib HTTP 路由（含 /api/state、/api/timeline、分块字节流）
server/seed.py    通过真实 HTTP API 端到端播种
server/test_acceptance.py 验收用例
static/           Leaflet 地图 SPA（工作台/冲突/待补/报告/移动录入）
data/             运行期：app.db、objects/（sha256 内容寻址）、chunks/、exports/
```

## 关键领域规则（本系统强制保证）

### 1. 三类时间严格分开
| 字段 | 含义 | 由谁写 |
|---|---|---|
| `flights.flight_date` | **飞行日期**（日历日） | 建架次时 |
| `media.captured_at` | **拍摄/录音时刻**（EXIF/设备，离线可改） | 采集端显式填写 |
| `media.uploaded_at` | **上传完成时刻** | 服务端在对象校验通过后写 |

时间线分泳道：飞行 / 拍摄 / 上传 / 发现 / 转写晚到，互相不回写、不混用。同一张照片 9-20 拍、9-22 传，能在两条泳道分别看到。

### 2. 镜头画面坐标 ≠ 地理坐标
证据必须同时给：帧内归一化坐标 `frame_cx/frame_cy∈[0,1]`、FOV、无人机经纬高、地面高程（DEM 来源）、`yaw/pitch/roll`、水平定位精度 `drone_hrms`。
`geo.py` 用针孔相机向量（光轴 d0 + right·tanα + up·tanβ）与地面平面求交，输出地面点、四角 footprint、误差圆。地图上**蓝点是无人机位置，橙点才是投影目标点**，两者在斜视时可相距上百米，绝不允许把帧像素直接当经纬度。

误差预算（1σ 正交合成）：水平 HRMS、高程/DEM 误差经斜视角放大 `σv·tanθ`、2 像素指向误差 `2H·px/cos²θ`、3m 底噪。关联地块时 **`locate_basis`（定位依据）与 `locate_error_m`（误差）必填**，缺失直接 400。

### 3. 同一发现可有多次巡检证据；图像相似不自动合并
`findings 1—N inspections N—N finding_evidence N—1 evidence`。9-20 首轮与 9-28 雨后复检是**两个独立轮次**，各自挂证据。
位置临近 / 图像相似只会在“冲突合并中心”产生一条 **open 冲突（带距离与相似度提示）**；同一巡检轮次两人标注同处也产生冲突。**必须人工选保留方 + 写处理说明**；合并时被合并方的所有巡检轮次整体迁移到保留方（证据不丢），也可判为“保留为不同事件”。系统从不自动合并。

### 4. 两种录入工作流都支持
- **传完素材再录发现**：分块传完 → 建证据 → 建发现/轮次。
- **先建带占位附件的发现**：`pending_attachments` 占位（发现进入 `pending_evidence` 状态）→ 现场断网排队 → 联网续传，或用另一台设备补同一 `finding_id`；占位生命周期 `placeholder → uploaded / superseded`。

移动端：IndexedDB 存 Blob、localStorage 存元数据；恢复网络后逐块上传，分块 idx 幂等（断网/崩溃/重复点都安全）。

### 5. 上传与对象存储
- 分块：`POST /api/uploads`（init，返回 `received` 已收块与 `instant` 秒传标记）→ `POST /api/uploads/{sid}/chunks/{idx}`（原始字节 + `X-Chunk-Sha256`）。
- **块重复**：同 (session, idx) 已存在直接返回 `dedup:true` 跳过。
- **整对象秒传**：sha256 在对象存储已存在则零字节完成，两个 media 记录共享同一 `object_key`（不重复占空间）。
- 拼装后整体 sha256 复核；对象按 `objects/ab/cd/<sha>.<ext>` 内容寻址。

### 6. 视频裁切与时间码
`clips` 保存 `src_start_ms/src_end_ms` 与源时间码 `start_tc/end_tc`、`cut_revision`。证据引用片段时 `frame_time_ms` 始终是**源视频时间码**——裁切后片段本地从 00:00 开始也要能映射回源，避免“裁切改变时间码”导致定位错位。

### 7. 语音转写晚到
上传完成即入队（后台 worker 模拟 2–6s 返回），`transcript_status: queued→done`，时间线单列“转写晚到”轨道，不覆盖拍摄/上传时间；前端 8s 轮询自动出现。

### 8. 地块边界版本与发现重判
每次修订新增 `plot_versions`（带 RMS、来源、原因、修订人），`plots.active_version` 指向当前。
修订后全量重判：仍在同一地块仅刷新版本引用（不算变更）；**跨地块（含 有↔无）才记入 `finding_plot_history`**。

### 9. 已派出复核保留原报告引用
`review_tasks` 在派出时快照 `report_code_snap / report_version_snap`。之后报告再版、地块重判都**不改写**该引用——复核员看到的永远是派出那一刻的报告。

### 10. 报告 / 预览包 / 导出
- 预览包**只包含 status=reviewed 的发现 + 用户手填建议**，并带免责声明；系统不写入、不推荐任何自动农事处置结论（`suggestions` 只能由用户 POST 写入）。
- 导出 ZIP 逐对象检查对象存储：缺失（未传完 / 对象丢失，含秒传共享对象失效）进入 `missing_json` 清单，导出标 `missing`，而不是静默漏素材。

## 主要 API
`GET /api/state` 一次拉全量 · `GET /api/timeline?lane=` · 地块 `POST /api/plots`、`POST /api/plots/{id}/revisions`
飞行 `POST /api/flights` · 上传 `POST /api/uploads`、`POST /api/uploads/{sid}/chunks/{idx}`、`GET /api/uploads/{sid}`
证据 `POST /api/evidence` · 片段 `POST /api/media/{id}/clips` · 媒体字节 `GET /api/media/{id}/raw`（未传 409 / 对象丢失 410）
发现 `POST /api/findings`、`.../inspections`、`.../review`、`.../suggestions`、`.../status`
冲突 `GET /api/conflicts`、`POST /api/conflicts/{id}/resolve`
占位 `POST /api/pending`、`PATCH /api/pending/{id}`
报告 `POST /api/reports/generate`、`/api/reports/{id}/dispatch`、`/api/reports/{id}/export`
复核 `PATCH /api/reviews/{id}` · 转写晚到 `PATCH /api/media/{id}/transcript`

## 验收对照（test_acceptance.py，19 项全绿）
块重复幂等、整对象秒传、坏块校验拒绝、语音转写晚到、视频源时间码、两人标注同处冲突、图像相似不自动合并、多轮巡检独立保留、定位依据+误差必填、帧坐标严格投影、边界修订跨地块重判、已派出复核保留报告快照、预览包仅审核发现+用户建议无自动处置、导出缺件清单、占位附件先建后补。
