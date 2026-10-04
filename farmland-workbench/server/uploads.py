# -*- coding: utf-8 -*-
"""
分块上传会话: 断网续传、跨设备补充、重复块幂等。

- 会话带 resume_token, 任何设备凭 token 查询已收块并续传。
- PUT 块幂等: 同 index 同 sha -> 200 (已存在, 不重复写); 同 index 异 sha -> 409。
- complete: 按序拼接 -> 对象存储 (内容寻址) -> 校验整体 sha -> asset 置 complete,
  upload_time 落服务端时间 (与 capture_time/flight_date 严格分离)。
"""
import hashlib
import uuid

import storage
from db import db, now_ms


def create_session(asset_id, filename, total_size, total_chunks, whole_sha256,
                   device_id=None):
    conn = db()
    sid = uuid.uuid4().hex
    token = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO upload_sessions
           (id, asset_id, resume_token, filename, total_size, total_chunks,
            whole_sha256, device_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sid, asset_id, token, filename, total_size, total_chunks,
         whole_sha256, device_id, now_ms()))
    conn.execute("UPDATE media_assets SET status='uploading' WHERE id=?", (asset_id,))
    conn.commit()
    return sid, token


def get_session_by_token(token):
    row = db().execute(
        "SELECT * FROM upload_sessions WHERE resume_token=?", (token,)).fetchone()
    return row


def received_chunks(session_id):
    rows = db().execute(
        "SELECT chunk_index, chunk_sha256, size_bytes FROM upload_chunks "
        "WHERE session_id=? ORDER BY chunk_index", (session_id,)).fetchall()
    return {r["chunk_index"]: dict(r) for r in rows}


def put_chunk(session_id, index, data, device_id=None):
    """幂等写块。返回 (status_code, payload)。"""
    conn = db()
    s = conn.execute("SELECT * FROM upload_sessions WHERE id=?",
                     (session_id,)).fetchone()
    if s is None:
        return 404, {"error": "session_not_found"}
    if s["status"] != "open":
        return 409, {"error": "session_closed", "status": s["status"]}
    if not (0 <= index < s["total_chunks"]):
        return 400, {"error": "chunk_index_out_of_range"}

    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute(
        "SELECT * FROM upload_chunks WHERE session_id=? AND chunk_index=?",
        (session_id, index)).fetchone()
    # 幂等: 同一位置重复传完全相同的块 -> 直接成功 (验收: 上传块重复)
    if existing:
        if existing["chunk_sha256"] != sha:
            return 409, {"error": "chunk_conflict",
                         "chunk_index": index,
                         "existing_sha256": existing["chunk_sha256"],
                         "received_sha256": sha}
        return 200, {"chunk_index": index, "duplicate": True, "sha256": sha}

    # 内容寻址: 不同会话相同块也复用同一对象, 但本会话首次写入该序号
    key = storage.put_bytes(data)
    conn.execute(
        """INSERT INTO upload_chunks
           (id, session_id, chunk_index, chunk_sha256, object_key, size_bytes,
            received_at, device_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (uuid.uuid4().hex, session_id, index, sha, key, len(data),
         now_ms(), device_id))
    conn.commit()
    return 200, {"chunk_index": index, "duplicate": False, "sha256": sha}


def complete_session(session_id):
    conn = db()
    s = conn.execute("SELECT * FROM upload_sessions WHERE id=?",
                     (session_id,)).fetchone()
    if s is None:
        return 404, {"error": "session_not_found"}
    if s["status"] == "completed":
        return 200, {"asset_id": s["asset_id"], "already_completed": True}

    chunks = conn.execute(
        "SELECT object_key, size_bytes FROM upload_chunks "
        "WHERE session_id=? ORDER BY chunk_index", (session_id,)).fetchall()
    if len(chunks) != s["total_chunks"]:
        missing = sorted(set(range(s["total_chunks"])) -
                         {r["chunk_index"] for r in conn.execute(
                             "SELECT chunk_index FROM upload_chunks WHERE session_id=?",
                             (session_id,))})
        return 409, {"error": "missing_chunks", "missing": missing}

    def stream():
        for c in chunks:
            with storage.open_ro(c["object_key"]) as f:
                while True:
                    buf = f.read(1 << 20)
                    if not buf:
                        break
                    yield buf

    try:
        whole_key = storage.put_stream_iter(stream(), s["whole_sha256"])
    except ValueError as e:
        return 422, {"error": str(e)}

    total = sum(c["size_bytes"] for c in chunks)
    ts = now_ms()
    conn.execute(
        "UPDATE upload_sessions SET status='completed', completed_at=? WHERE id=?",
        (ts, session_id))
    conn.execute(
        """UPDATE media_assets
           SET status='complete', sha256=?, size_bytes=?, upload_time=?
           WHERE id=?""",
        (whole_key, total, ts, s["asset_id"]))
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity_type, entity_id, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (s["device_id"], "upload.complete", "media_asset", s["asset_id"],
         "size=%d sha=%s" % (total, whole_key), ts))
    conn.commit()
    return 200, {"asset_id": s["asset_id"], "sha256": whole_key,
                 "size_bytes": total, "upload_time": ts}


def abort_session(session_id):
    db().execute("UPDATE upload_sessions SET status='abandoned' WHERE id=?",
                 (session_id,))
    db().commit()
    return 200, {"status": "abandoned"}
