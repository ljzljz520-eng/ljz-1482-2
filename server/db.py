# -*- coding: utf-8 -*-
"""SQLite relational store + content-addressed object storage paths."""
import os, sqlite3, threading, time, uuid as uuidlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "app.db")
OBJ_DIR = os.path.join(DATA, "objects")
CHUNK_DIR = os.path.join(DATA, "chunks")
EXPORT_DIR = os.path.join(DATA, "exports")
for d in (DATA, OBJ_DIR, CHUNK_DIR, EXPORT_DIR):
    os.makedirs(d, exist_ok=True)

_lock = threading.RLock()

SCHEMA = r"""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, device_id TEXT);

CREATE TABLE IF NOT EXISTS plots(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  active_version INTEGER DEFAULT 1, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS plot_versions(
  id INTEGER PRIMARY KEY, plot_id INTEGER NOT NULL REFERENCES plots(id),
  version INTEGER NOT NULL, boundary_geojson TEXT NOT NULL,
  area_mu REAL, source TEXT, rms_error_m REAL, revised_reason TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  UNIQUE(plot_id, version));

CREATE TABLE IF NOT EXISTS flights(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, drone_model TEXT, camera_model TEXT,
  pilot TEXT, flight_date TEXT NOT NULL,        -- 飞行日期 (date, 与拍摄时刻分开)
  takeoff_at TEXT,                             -- 起飞/拍摄时间
  footprint_geojson TEXT, note TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS media(
  id INTEGER PRIMARY KEY, client_uid TEXT UNIQUE, kind TEXT NOT NULL, -- image|video|audio
  flight_id INTEGER REFERENCES flights(id),
  filename TEXT, mime TEXT, size INTEGER DEFAULT 0, sha256 TEXT,
  object_key TEXT,                            -- 对象存储 key (内容寻址)
  status TEXT NOT NULL DEFAULT 'uploading',   -- uploading|uploaded|missing
  duration_ms INTEGER, width INTEGER, height INTEGER,
  captured_at TEXT,                           -- 拍摄/录音时间 (必须显式提供)
  uploaded_at TEXT,                           -- 上传完成时间 (服务端填写)
  uploader_id INTEGER REFERENCES users(id),
  transcript TEXT, transcript_status TEXT,    -- 语音转写: queued|done|failed
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS upload_sessions(
  id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL REFERENCES media(id),
  client_uid TEXT UNIQUE NOT NULL, chunk_size INTEGER NOT NULL,
  total_size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  pending_id INTEGER,
  status TEXT NOT NULL DEFAULT 'open',        -- open|completed|aborted
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS chunks(
  session_id INTEGER NOT NULL REFERENCES upload_sessions(id),
  idx INTEGER NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  received_at TEXT NOT NULL, PRIMARY KEY(session_id, idx));

CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL REFERENCES media(id),
  clip_id INTEGER,
  frame_time_ms INTEGER,                       -- 视频内帧时间码
  frame_cx REAL, frame_cy REAL,                -- 镜头画面归一化坐标 0..1 (禁止当地理坐标)
  fov_h REAL, fov_v REAL,
  drone_lng REAL, drone_lat REAL, drone_alt REAL, ground_elevation REAL,
  yaw REAL, pitch REAL, roll REAL, camera_model TEXT,
  dem_source TEXT, drone_hrms REAL,
  footprint_geojson TEXT, center_lng REAL, center_lat REAL, radius_m REAL,
  horizontal_error_m REAL, geo_method TEXT,
  computed_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL REFERENCES media(id),
  src_start_ms INTEGER NOT NULL, src_end_ms INTEGER NOT NULL,
  start_tc TEXT NOT NULL, end_tc TEXT NOT NULL,
  cut_revision INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, note TEXT);

CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  ftype TEXT, severity TEXT,
  status TEXT NOT NULL DEFAULT 'draft',        -- draft|pending_evidence|reviewed|merged
  plot_id INTEGER REFERENCES plots(id),
  plot_version_id INTEGER REFERENCES plot_versions(id),
  location_lng REAL, location_lat REAL, geometry_geojson TEXT,
  locate_basis TEXT NOT NULL,                  -- 定位依据 (必填)
  locate_error_m REAL NOT NULL,                -- 定位误差 (必填)
  first_seen_flight_id INTEGER REFERENCES flights(id),
  first_seen_at TEXT, reviewed_by INTEGER, reviewed_at TEXT,
  merged_into_id INTEGER, merge_note TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS inspections(
  id INTEGER PRIMARY KEY, finding_id INTEGER NOT NULL REFERENCES findings(id),
  flight_id INTEGER NOT NULL REFERENCES flights(id),
  inspected_at TEXT NOT NULL,                  -- 该次巡检时间
  note TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS finding_evidence(
  id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id),
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  annotated_by INTEGER REFERENCES users(id), note TEXT, created_at TEXT NOT NULL,
  UNIQUE(inspection_id, evidence_id));

CREATE TABLE IF NOT EXISTS finding_plot_history(
  id INTEGER PRIMARY KEY, finding_id INTEGER NOT NULL REFERENCES findings(id),
  plot_id INTEGER, plot_version_id INTEGER, inside INTEGER NOT NULL,
  reason TEXT, changed_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY, finding_a INTEGER NOT NULL REFERENCES findings(id),
  finding_b INTEGER NOT NULL REFERENCES findings(id),
  reason TEXT NOT NULL, distance_m REAL, image_similarity REAL,
  status TEXT NOT NULL DEFAULT 'open',         -- open|merged|kept_separate
  survivor_id INTEGER, resolution_note TEXT,
  resolved_by INTEGER, resolved_at TEXT, created_at TEXT NOT NULL);

-- 占位附件: 先建发现后补素材 / 断网 / 跨设备
CREATE TABLE IF NOT EXISTS pending_attachments(
  id INTEGER PRIMARY KEY, client_uid TEXT UNIQUE NOT NULL,
  owner_device TEXT, kind TEXT, filename TEXT, note TEXT,
  finding_client_ref TEXT, finding_id INTEGER REFERENCES findings(id),
  media_id INTEGER REFERENCES media(id),
  status TEXT NOT NULL DEFAULT 'placeholder',  -- placeholder|uploaded|superseded
  created_at TEXT NOT NULL, synced_at TEXT, uploaded_at TEXT);

CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'draft',
  config_json TEXT, package_json TEXT,
  generated_by INTEGER REFERENCES users(id), generated_at TEXT,
  dispatched_at TEXT);
CREATE TABLE IF NOT EXISTS report_findings(
  report_id INTEGER NOT NULL REFERENCES reports(id),
  finding_id INTEGER NOT NULL REFERENCES findings(id),
  snap_plot_id INTEGER, snap_plot_version_id INTEGER,
  snap_location_lng REAL, snap_location_lat REAL,
  PRIMARY KEY(report_id, finding_id));

CREATE TABLE IF NOT EXISTS review_tasks(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, finding_id INTEGER NOT NULL,
  -- 派出时刻快照: 之后报告再版/地块修订都不改变该引用
  dispatched_from_report_id INTEGER REFERENCES reports(id),
  report_code_snap TEXT, report_version_snap INTEGER,
  assigned_to INTEGER REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending',      -- pending|done|rejected
  note TEXT, due_at TEXT, created_at TEXT NOT NULL, completed_at TEXT);

CREATE TABLE IF NOT EXISTS suggestions(
  id INTEGER PRIMARY KEY, finding_id INTEGER NOT NULL REFERENCES findings(id),
  author_id INTEGER REFERENCES users(id), content TEXT NOT NULL,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS exports(
  id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id),
  status TEXT NOT NULL,                       -- running|done|missing|failed
  package_path TEXT, missing_json TEXT, file_count INTEGER,
  created_by INTEGER, created_at TEXT NOT NULL, completed_at TEXT);
"""

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def new_uuid():
    return uuidlib.uuid4().hex

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    with _lock:
        c = connect()
        try:
            c.executescript(SCHEMA)
            if not c.execute("SELECT 1 FROM users").fetchone():
                c.executescript("""
                INSERT INTO users(id,name,role,device_id) VALUES
                 (1,'王农技','agronomist','dev-A'),
                 (2,'李巡检','scout','dev-B'),
                 (3,'张站长','manager','desk-1');""")
            c.commit()
        finally:
            c.close()

def db():
    """Caller gets a short-lived connection."""
    return connect()

def lock():
    return _lock

def object_path(key):
    return os.path.join(OBJ_DIR, key)

def object_key_for(sha256, ext="bin"):
    return os.path.join(sha256[:2], sha256[2:4], sha256 + "." + ext)

def chunk_path(session_id, idx):
    d = os.path.join(CHUNK_DIR, str(session_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%08d.part" % idx)

def rows_to_dicts(rows):
    return [dict(r) for r in rows]
