# -*- coding: utf-8 -*-
"""
关系库层: SQLite (标准库 sqlite3, WAL)。

设计要点:
- 三类时间严格分离:
    flights.flight_date   飞行日期 (作业当天)
    media_assets.capture_time  镜头拍摄时刻 (素材自带/设备时钟)
    media_assets.upload_time   服务端收到完整对象的时刻 (服务端时钟)
- 镜头画面坐标 (像素 px) 只存 evidences.pixel_x/pixel_y, 绝不直接当作地理坐标。
  地理坐标必须带定位依据 (geo_method) 与误差 (geo_error_m), 应用层强制校验。
- 地块边界版本化 (plot_versions); 发现归属随修订重算并留痕 (attribution_history)。
- 同一发现可有多次巡检证据 (evidences -> inspection_events)。系统不提供任何按
  图像相似度合并发现/事件的逻辑; sha256 仅用于对象存储内容去重。
"""
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get(
    "WORKBENCH_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "workbench.db"))

_lock = threading.Lock()
_conn = None


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    global _conn
    with _lock:
        if _conn is None:
            _conn = connect()
            _conn.executescript(SCHEMA)
            _conn.commit()
    return _conn


def db():
    if _conn is None:
        init()
    return _conn


def now_ms():
    return int(time.time() * 1000)


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS devices (
  device_id    TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  platform     TEXT,
  registered_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plots (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  crop TEXT,
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plot_versions (
  id TEXT PRIMARY KEY,
  plot_id TEXT NOT NULL REFERENCES plots(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  -- GeoJSON Polygon, 坐标一律为 [lon, lat] WGS84
  geometry TEXT NOT NULL,
  source TEXT NOT NULL,                 -- import / survey / edit
  change_note TEXT,
  edited_by TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(plot_id, version)
);

CREATE TABLE IF NOT EXISTS flights (
  id TEXT PRIMARY KEY,
  flight_date TEXT NOT NULL,           -- 仅飞行日期 YYYY-MM-DD
  pilot TEXT,
  aircraft TEXT,
  note TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media_assets (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('image','video','audio')),
  filename TEXT,
  content_type TEXT,
  size_bytes INTEGER,
  sha256 TEXT,                          -- 完整对象 sha256; NULL=占位/未完成
  flight_id TEXT REFERENCES flights(id),
  device_id TEXT REFERENCES devices(device_id),
  capture_time INTEGER,                 -- 拍摄时间 (设备时钟, 毫秒)
  upload_time INTEGER,                  -- 上传完成时间 (服务端时钟, 毫秒)
  duration_ms INTEGER,
  width INTEGER,
  height INTEGER,
  status TEXT NOT NULL DEFAULT 'pending_placeholder'
      CHECK(status IN ('pending_placeholder','uploading','complete','missing','quarantined')),
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_flight ON media_assets(flight_id);
CREATE INDEX IF NOT EXISTS idx_media_sha ON media_assets(sha256);

CREATE TABLE IF NOT EXISTS upload_sessions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  resume_token TEXT NOT NULL UNIQUE,
  filename TEXT,
  total_size INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  whole_sha256 TEXT,
  device_id TEXT REFERENCES devices(device_id),
  status TEXT NOT NULL DEFAULT 'open'
      CHECK(status IN ('open','completed','abandoned')),
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_session_token ON upload_sessions(resume_token);

CREATE TABLE IF NOT EXISTS upload_chunks (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  chunk_sha256 TEXT NOT NULL,
  object_key TEXT NOT NULL,            -- 内容寻址 key
  size_bytes INTEGER NOT NULL,
  received_at INTEGER NOT NULL,
  device_id TEXT,
  UNIQUE(session_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS video_segments (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  source_segment_id TEXT REFERENCES video_segments(id),
  source_start_ms INTEGER NOT NULL,    -- 相对源素材的时间码
  source_end_ms INTEGER NOT NULL,
  timecode_offset_ms INTEGER NOT NULL DEFAULT 0, -- 裁切后的 t' = t - offset
  name TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seg_asset ON video_segments(asset_id);

CREATE TABLE IF NOT EXISTS transcriptions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  text TEXT NOT NULL DEFAULT '',
  provider TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','ready','failed')),
  ordered_at INTEGER NOT NULL,         -- 转写请求时间
  ready_at INTEGER                      -- 转写晚到时间
);
CREATE INDEX IF NOT EXISTS idx_tx_asset ON transcriptions(asset_id);

CREATE TABLE IF NOT EXISTS inspection_events (
  id TEXT PRIMARY KEY,
  flight_id TEXT REFERENCES flights(id),
  event_date TEXT NOT NULL,            -- 巡检日期 (可与飞行日期不同, 如地面补巡)
  kind TEXT NOT NULL DEFAULT 'aerial'
      CHECK(kind IN ('aerial','ground','drone_recheck')),
  inspector TEXT,
  note TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  category TEXT,
  severity TEXT,
  status TEXT NOT NULL DEFAULT 'needs_evidence'
      CHECK(status IN ('needs_evidence','pending_review','reviewed','disputed')),
  -- 经像素->地理合法通道得出的坐标 (WGS84)
  lon REAL,
  lat REAL,
  geo_method TEXT CHECK(geo_method IS NULL OR geo_method IN
      ('exif_gps','manual_pin','ortho_match','frame_projection')),
  geo_error_m REAL,
  geo_note TEXT,                         -- 定位依据说明
  plot_id TEXT REFERENCES plots(id),    -- 归属地块 (可空)
  attribution_status TEXT CHECK(attribution_status IS NULL OR attribution_status IN
      ('auto_inside','auto_buffer','manual','none')),
  plot_version INTEGER,                 -- 归属判断时使用的地块版本
  created_by TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_find_plot ON findings(plot_id);

CREATE TABLE IF NOT EXISTS evidences (
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  event_id TEXT REFERENCES inspection_events(id),  -- 多次巡检证据各自挂事件
  asset_id TEXT REFERENCES media_assets(id),
  segment_id TEXT REFERENCES video_segments(id),
  -- 镜头画面坐标 (像素!), 仅用于在画面中标注, 不能直接当地理坐标
  pixel_x REAL,
  pixel_y REAL,
  frame_width INTEGER,
  frame_height INTEGER,
  note TEXT,
  created_by TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_finding ON evidences(finding_id);
CREATE INDEX IF NOT EXISTS idx_ev_event ON evidences(event_id);

CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY,
  finding_id TEXT REFERENCES findings(id) ON DELETE CASCADE,
  event_id TEXT REFERENCES inspection_events(id),
  author TEXT NOT NULL,
  lon REAL NOT NULL,
  lat REAL NOT NULL,
  geo_method TEXT NOT NULL,
  geo_error_m REAL,
  label TEXT,
  comment TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                   -- same_spot / merge_candidate / attribution_changed
  ref_a TEXT,                           -- annotation / finding id
  ref_b TEXT,
  distance_m REAL,
  status TEXT NOT NULL DEFAULT 'open'
      CHECK(status IN ('open','resolved_keep_both','resolved_merged','resolved_dismissed')),
  resolution_note TEXT,
  resolved_by TEXT,
  resolved_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_proposals (
  id TEXT PRIMARY KEY,
  source_finding_id TEXT NOT NULL REFERENCES findings(id),
  target_finding_id TEXT NOT NULL REFERENCES findings(id),
  reason TEXT,
  -- 明确标记: 系统绝不因图像相似自动合并; 仅记录人工依据
  created_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed'
      CHECK(status IN ('proposed','accepted','rejected')),
  created_at INTEGER NOT NULL,
  decided_at INTEGER
);

CREATE TABLE IF NOT EXISTS attribution_history (
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  old_plot_id TEXT,
  new_plot_id TEXT,
  old_version INTEGER,
  new_version INTEGER,
  old_status TEXT,
  new_status TEXT,
  reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL DEFAULT 'draft'
      CHECK(status IN ('draft','published')),
  created_by TEXT,
  created_at INTEGER NOT NULL,
  published_at INTEGER
);

CREATE TABLE IF NOT EXISTS report_findings (
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  PRIMARY KEY(report_id, finding_id)
);

-- 已派出的复核事项: 保留原报告引用 (original_report_id 不因重判/修订而改写)
CREATE TABLE IF NOT EXISTS review_tasks (
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id),
  original_report_id TEXT REFERENCES reports(id),
  assignee TEXT,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'assigned'
      CHECK(status IN ('assigned','done','cancelled')),
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);

-- 用户填写的农事建议 (预览包只携带它; 系统不自动决定处置)
CREATE TABLE IF NOT EXISTS preview_suggestions (
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  report_id TEXT REFERENCES reports(id) ON DELETE CASCADE,
  suggestion TEXT NOT NULL,
  decision TEXT,                          -- 用户处置决定 (留空=未决定)
  created_by TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS preview_packages (
  id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  object_key TEXT,                       -- 预览 zip
  manifest TEXT,                         -- 打包时清单快照
  only_reviewed INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS export_jobs (
  id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES reports(id),
  status TEXT NOT NULL DEFAULT 'requested'
      CHECK(status IN ('requested','complete','failed')),
  object_key TEXT,
  missing_items TEXT,                    -- 导出时缺失素材清单 (JSON)
  log TEXT,
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS client_drafts (
  id TEXT PRIMARY KEY,
  device_id TEXT REFERENCES devices(device_id),
  payload TEXT NOT NULL,                 -- 移动端离线录入快照 JSON
  resume_tokens TEXT,                   -- 关联上传会话 token 列表 JSON
  share_code TEXT,                       -- 跨设备补充码
  status TEXT NOT NULL DEFAULT 'open'
      CHECK(status IN ('open','synced','superseded')),
  created_at INTEGER NOT NULL,
  synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  detail TEXT,
  created_at INTEGER NOT NULL
);
"""
