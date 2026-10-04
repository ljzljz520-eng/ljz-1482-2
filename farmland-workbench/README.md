# 航拍农田报告工作台

Web 地图与时间线联动地块 / 发现项 / 视频片段；移动端录入图片与语音（可离线、分块续传、跨设备补充）；
服务端使用 **SQLite 关系库 + 内容寻址对象存储（本地文件模拟 S3）** 维护上传、空间版本与报告。
纯 Python 标准库 + 原生前端（地图为自研 SVG，无需任何 CDN/底图密钥）。

## 启动

```bash
cd farmland-workbench
./run.sh                 # 或: python3 server/app.py
# 打开 http://localhost:8080
```

首次启动自动建库并种入演示数据（1 块农田 / 2 次飞行巡检 / 4 个发现 / 语音转写等待 /
占位附件 / 两人标注冲突 / 已发布报告与预览包）。

验收测试（自启临时库与 8091 端口，不污染演示数据）：

```bash
python3 server/accept_test.py    # 49 项断言
```

## 页面

| 页面 | 功能 |
|---|---|
| `/index.html` | 地图（地块版本/发现点/误差圈/多人标注）与三轨时间线（飞行日期/拍摄/上传）联动；点击看发现的多次巡检证据、视频片段、归属变更留痕；一键模拟边界修订与重判 |
| `/capture.html` | 手机端：拍照/选视频/录音；三种时间分开；定位依据必选（人工刺点/EXIF GNSS/镜头投影）；像素坐标仅画面刺点；两种录入策略；离线队列、resume token 续传、8 位补充码跨设备拉取草稿 |
| `/conflicts.html` | 两人同处标注冲突（保留双标/人工合并/忽略）；人工合并建议（拒绝/接受证据迁移）；待补证据与占位/缺失素材；语音转写晚到模拟；导出缺失校验 |
| `/report.html` | 报告发现清单（审核状态明示预览排除）；用户手工填写建议/决定；发布；预览包；完整导出；派出复核事项（保留原报告引用） |

## 关键规则与实现位置

### 1. 三类时间严格分开
- `flights.flight_date`（作业日）、`media_assets.capture_time`（设备时钟）、
  `media_assets.upload_time`（服务端在对象完整到达时写入）。
- 时间线按四轨渲染：`flight_date / capture_time / upload_time / processing`（见 `services.timeline`）。

### 2. 镜头画面坐标不能直接当地理坐标
- 像素只存在 `evidences.pixel_x/pixel_y`，与证据（画面）同行。
- 地理坐标必须带 `geo_method ∈ {exif_gps, manual_pin, ortho_match, frame_projection}`
  和 `geo_error_m`；`geo.validate_geo` 在落库前统一拦截裸坐标（400）。
- `geo.frame_projection`：无人机位置 + yaw/pitch/roll + 焦距像素 + 像素，
  按相机模型投影到平面地面，并显式传播 GNSS/姿态/1px/地形高度误差，输出综合误差（米）。

### 3. 关联地块要明确定位依据与误差
- 归属三态：`auto_inside`（界内）、`auto_buffer`（到边界距离 ≤ 定位误差，**需人工确认**）、`none`；
  人工确认后为 `manual`（锁定，后续修订不自动覆盖）。判断时记录 `plot_version`。

### 4. 同一发现的多次巡检证据，不按图像相似合并
- 每条证据挂 `inspection_events`（航巡/地面/复核）；同一发现可含不同日期事件的多条证据。
- 系统没有任何图像相似度/自动合并路径；只有 `merge_proposals` 人工提议，
  接受时迁移证据并把源发现置 `disputed`（不物理删除，审计可追溯）。

### 5. 两种录入策略 + 断网续传 + 跨设备补充
- 模式 A：先建 asset+分块会话，传完 complete，再挂发现证据。
- 模式 B：先建 `pending_placeholder` asset 并挂发现（保持 `needs_evidence`），
  之后 `POST /api/assets/{id}/upload-session` 在任意设备续传。
- 上传：`resume_token` 查询已收块；同 index 同 sha 幂等（200 duplicate），同 index 异 sha 409；
  块内容寻址，跨会话/跨设备相同块直接复用；complete 时按序拼接并校验整体 sha256。
- 移动端离线：localStorage 队列（含 base64 素材），恢复在线后凭 token 自动续传；
  `client_drafts.share_code` 支持另一台设备拉取草稿补充上传。

### 6. 边界修订 → 归属重判；复核保留原报告引用
- `plot_versions` 只追加、不可变；修订后 `reattribute_after_revision` 对所有有坐标的发现重判，
  写入 `attribution_history`（旧/新地块、旧/新版本、旧/新状态、原因）。
- `review_tasks.original_report_id` 在派出时固定，重判/修订不回写。

### 7. 上传块重复 / 语音转写晚到 / 视频裁切时间码
- 块重复：见第 5 点（幂等 + 409 冲突），验收脚本 A 段。
- 转写：`transcriptions` 有 `ordered_at / ready_at`，素材早已 complete、转写异步到达时只更新转写并刷新发现状态。
- 视频：`video_segments` 记录 `source_start/end_ms` 与 `timecode_offset_ms`，
  新时间码 `t' = t − offset`，源素材不动、引用可回溯（`remap_timecode`）。

### 8. 两人标注同处 / 导出中素材缺失 / 待补与冲突状态
- 标注按人各自留存；后到标注若与他人同事件距离 ≤ 30m，生成 `conflicts(same_spot, open)`，
  网页提供保留双标 / 人工合并 / 忽略。
- 导出逐证据核对 `asset.status == complete` 且对象存储存在；失败写 `export_jobs.missing_items`，网页列出待补清单。
- 发现状态机 `needs_evidence → pending_review → reviewed/disputed`，
  无完整素材证据时禁止审核（400 门禁）。

### 9. 预览包只含已审核发现 + 用户填写建议
- `build_preview_package` 仅打包 `reviewed` 发现，未审核项写入 `excluded_not_reviewed`；
- 包内 `manifest.json / preview.html / suggestions.md / README.txt` 只携带用户手工填写的建议与决定，
  系统不输出任何自动农事处置（README 明示"不构成农事处置决定"）。

## 主要 API（节选）

```
POST /api/devices
POST /api/plots | GET /api/plots | POST /api/plots/{id}/revisions
POST /api/flights | POST /api/events
POST /api/assets                       # 正常(分块) 或 {placeholder:true}
POST /api/assets/{id}/upload-session  # 占位附件续传
PUT  /api/uploads/{resumeToken}/chunks/{index}   # 幂等
GET  /api/uploads/{resumeToken}                  # 已收块(断网查询)
POST /api/uploads/{resumeToken}/complete
POST /api/assets/{id}/transcription              # 下单 或 {text} 晚到到达
POST /api/assets/{id}/segments                   # 视频裁切(时间码偏移)
POST /api/findings        # camera{...} 走投影; pixel_* 只存证据行
POST /api/findings/{id}/evidence | /status | /plot
POST /api/annotations | GET /api/conflicts | POST /api/conflicts/{id}/resolve
POST /api/merge-proposals | POST /api/merge-proposals/{id}/decide
POST /api/reports | GET /api/reports/{id}/detail
POST /api/reports/{id}/findings | /suggestions | /publish
POST /api/reports/{id}/preview | GET /api/preview/{id}
POST /api/reports/{id}/export | GET /api/exports/{id}
POST /api/reports/{id}/review-tasks
POST /api/drafts | GET /api/drafts/{shareCode}
GET  /api/timeline | /api/workbench
GET  /media/{sha256}     # 对象存储, 支持 Range
```

## 存储布局

```
data/workbench.db        # SQLite(WAL): 关系/版本/状态/审计
data/storage/objects/xx/<sha256>   # 内容寻址对象(块与完整素材同池)
data/storage/derived/    # 裁切/导出 zip/预览 zip
```
