# -*- coding: utf-8 -*-
"""
航拍农田报告工作台 - 服务端 (纯标准库)。
关系库: SQLite(db.py)  对象存储: storage.py  上传: uploads.py  地理: geo.py
运行: python3 app.py  (默认 :8080, 自动初始化; 无种子则种入演示数据)
"""
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import services
import storage
import uploads
from db import db, init, now_ms

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def jbody(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(n) if n else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "invalid_json")


def actor_of(handler, body=None):
    return (handler.headers.get("X-Actor") or (body or {}).pop("_actor", None)
            or "anonymous")


def device_of(handler, body=None):
    return handler.headers.get("X-Device-Id") or (body or {}).get("device_id")


class ApiError(Exception):
    def __init__(self, status, code, extra=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra or {}


# ---------------------------------------------------------------- routing
ROUTES = [
    ("POST", r"^/api/devices$"),
    ("GET", r"^/api/plots$"),
    ("POST", r"^/api/plots$"),
    ("GET", r"^/api/plots/(\w+)$"),
    ("POST", r"^/api/plots/(\w+)/revisions$"),
    ("POST", r"^/api/flights$"),
    ("GET", r"^/api/flights$"),
    ("POST", r"^/api/events$"),
    ("GET", r"^/api/events$"),

    ("POST", r"^/api/assets$"),
    ("GET", r"^/api/assets$"),
    ("GET", r"^/api/assets/(\w+)$"),
    ("POST", r"^/api/assets/(\w+)/upload-session$"),
    ("PUT", r"^/api/uploads/([0-9a-f]+)/chunks/(\d+)$"),
    ("GET", r"^/api/uploads/([0-9a-f]+)$"),
    ("POST", r"^/api/uploads/([0-9a-f]+)/complete$"),
    ("DELETE", r"^/api/uploads/([0-9a-f]+)$"),
    ("POST", r"^/api/assets/(\w+)/transcription$"),
    ("POST", r"^/api/assets/(\w+)/segments$"),
    ("POST", r"^/api/assets/(\w+)/missing$"),

    ("POST", r"^/api/findings$"),
    ("GET", r"^/api/findings$"),
    ("GET", r"^/api/findings/(\w+)$"),
    ("POST", r"^/api/findings/(\w+)/evidence$"),
    ("POST", r"^/api/findings/(\w+)/status$"),
    ("POST", r"^/api/findings/(\w+)/plot$"),
    ("POST", r"^/api/annotations$"),
    ("GET", r"^/api/annotations$"),
    ("GET", r"^/api/conflicts$"),
    ("POST", r"^/api/conflicts/(\w+)/resolve$"),
    ("POST", r"^/api/merge-proposals$"),
    ("GET", r"^/api/merge-proposals$"),
    ("POST", r"^/api/merge-proposals/(\w+)/decide$"),

    ("POST", r"^/api/reports$"),
    ("GET", r"^/api/reports$"),
    ("GET", r"^/api/reports/(\w+)/detail$"),
    ("POST", r"^/api/reports/(\w+)/findings$"),
    ("POST", r"^/api/reports/(\w+)/suggestions$"),
    ("POST", r"^/api/reports/(\w+)/publish$"),
    ("POST", r"^/api/reports/(\w+)/preview$"),
    ("GET", r"^/api/preview/(\w+)$"),
    ("POST", r"^/api/reports/(\w+)/export$"),
    ("GET", r"^/api/exports/(\w+)$"),
    ("POST", r"^/api/reports/(\w+)/review-tasks$"),
    ("GET", r"^/api/review-tasks$"),
    ("POST", r"^/api/review-tasks/(\w+)/complete$"),

    ("POST", r"^/api/drafts$"),
    ("GET", r"^/api/drafts/([0-9a-zA-Z]+)$"),
    ("POST", r"^/api/drafts/(\w+)/sync$"),

    ("GET", r"^/api/timeline$"),
    ("GET", r"^/api/workbench$"),
]


def match(method, path):
    for m, pat in ROUTES:
        if m == method:
            mm = re.match(pat + r"\Z", path)
            if mm:
                return pat, mm.groups()
    return None, None


# ---------------------------------------------------------------- handlers
def api_dispatch(method, path, args, body, h):
    def g(k, d=None):
        v = args.get(k)
        return v[0] if v else d

    if path == "/api/devices" and method == "POST":
        return {"device_id": services.register_device(
            body.get("name") or "未命名设备", body.get("platform"))}

    if path == "/api/plots" and method == "POST":
        pid = services.create_plot(body["name"], body.get("crop"), body["geometry"],
                                   source=body.get("source", "import"),
                                   note=body.get("note"), actor=actor_of(h, body))
        return {"plot_id": pid}
    if path == "/api/plots" and method == "GET":
        return {"plots": list_plots()}
    m = re.match(r"^/api/plots/(\w+)$", path)
    if m and method == "GET":
        return get_plot(m.group(1))
    m = re.match(r"^/api/plots/(\w+)/revisions$", path)
    if m and method == "POST":
        v = services.revise_plot(m.group(1), body["geometry"],
                                 body.get("change_note"), actor=actor_of(h, body))
        return {"version": v}

    if path == "/api/flights" and method == "POST":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", body["flight_date"]):
            raise ApiError(400, "bad_flight_date")
        return {"flight_id": services.create_flight(
            body["flight_date"], body.get("pilot"), body.get("aircraft"),
            body.get("note"), actor=actor_of(h, body))}
    if path == "/api/flights" and method == "GET":
        return {"flights": [dict(r) for r in db().execute(
            "SELECT * FROM flights ORDER BY flight_date").fetchall()]}
    if path == "/api/events" and method == "POST":
        return {"event_id": services.create_event(
            body.get("flight_id"), body["event_date"], body.get("kind", "aerial"),
            body.get("inspector"), body.get("note"), actor=actor_of(h, body))}
    if path == "/api/events" and method == "GET":
        return {"events": [dict(r) for r in db().execute(
            "SELECT * FROM inspection_events ORDER BY event_date").fetchall()]}

    # ---- assets & uploads ----
    if path == "/api/assets" and method == "POST":
        return create_asset(body, h)
    if path == "/api/assets" and method == "GET":
        rows = db().execute(
            """SELECT a.*, (SELECT status FROM upload_sessions us
                WHERE us.asset_id=a.id AND us.status='open' LIMIT 1) AS open_session
               FROM media_assets a ORDER BY a.created_at DESC""").fetchall()
        return {"assets": [dict(r) for r in rows]}
    m = re.match(r"^/api/assets/(\w+)$", path)
    if m and method == "GET":
        return get_asset_detail(m.group(1))
    m = re.match(r"^/api/assets/(\w+)/upload-session$", path)
    if m and method == "POST":
        return start_upload_for_placeholder(m.group(1), body, h)
    m = re.match(r"^/api/uploads/([0-9a-f]+)/chunks/(\d+)$", path)
    if m and method == "PUT":
        return put_chunk(m.group(1), int(m.group(2)), h)
    m = re.match(r"^/api/uploads/([0-9a-f]+)$", path)
    if m and method == "GET":
        return session_status(m.group(1))
    if m and method == "DELETE":
        return uploads.abort_session(_sid(m.group(1)))
    m = re.match(r"^/api/uploads/([0-9a-f]+)/complete$", path)
    if m and method == "POST":
        code, payload = uploads.complete_session(_sid(m.group(1)))
        if code == 200:
            services.refresh_findings_for_asset(payload["asset_id"])
            return payload
        raise ApiError(code, payload.get("error", "upload_error"), payload)
    m = re.match(r"^/api/assets/(\w+)/transcription$", path)
    if m and method == "POST":
        # body 可只 {status:"ordered"} 或 {text:...} 模拟晚到
        aid = m.group(1)
        if body.get("text") is not None:
            services.fulfill_transcription(aid, body["text"],
                                           body.get("provider", "mock-asr"),
                                           failed=body.get("failed", False))
            return {"status": "ready"}
        return {"transcription_id": services.order_transcription(aid)}
    m = re.match(r"^/api/assets/(\w+)/segments$", path)
    if m and method == "POST":
        sid = services.add_video_segment(
            m.group(1), int(body["source_start_ms"]), int(body["source_end_ms"]),
            body.get("name"), body.get("source_segment_id"))
        return {"segment_id": sid,
                "timecode_offset_ms": int(body["source_start_ms"])}
    m = re.match(r"^/api/assets/(\w+)/missing$", path)
    if m and method == "POST":
        db().execute("UPDATE media_assets SET status='missing' WHERE id=?",
                     (m.group(1),))
        db().commit()
        return {"status": "missing"}

    # ---- findings ----
    if path == "/api/findings" and method == "POST":
        try:
            fid = services.create_finding(body, actor=actor_of(h, body))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"finding_id": fid}
    if path == "/api/findings" and method == "GET":
        return {"findings": list_findings()}
    m = re.match(r"^/api/findings/(\w+)$", path)
    if m and method == "GET":
        return finding_detail(m.group(1))
    m = re.match(r"^/api/findings/(\w+)/evidence$", path)
    if m and method == "POST":
        try:
            eid = services.add_evidence(m.group(1), body, actor=actor_of(h, body))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"evidence_id": eid}
    m = re.match(r"^/api/findings/(\w+)/status$", path)
    if m and method == "POST":
        try:
            services.set_finding_status(m.group(1), body["status"],
                                        actor=actor_of(h, body), note=body.get("note"))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"ok": True}
    m = re.match(r"^/api/findings/(\w+)/plot$", path)
    if m and method == "POST":
        services.manual_set_plot(m.group(1), body.get("plot_id"),
                                 actor=actor_of(h, body), note=body.get("note"))
        return {"ok": True}

    if path == "/api/annotations" and method == "GET":
        return {"annotations": [dict(r) for r in db().execute(
            "SELECT * FROM annotations ORDER BY created_at").fetchall()]}
    if path == "/api/annotations" and method == "POST":
        try:
            aid, conflicts = services.add_annotation(body, actor=actor_of(h, body))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"annotation_id": aid, "conflicts": conflicts}
    if path == "/api/conflicts" and method == "GET":
        rows = db().execute(
            "SELECT * FROM conflicts ORDER BY status='open' DESC, created_at DESC"
        ).fetchall()
        out = []
        for c in rows:
            d = dict(c)
            for key in ("ref_a", "ref_b"):
                an = db().execute("SELECT * FROM annotations WHERE id=?",
                                  (d[key],)).fetchone()
                d[key + "_detail"] = dict(an) if an else None
            out.append(d)
        return {"conflicts": out}
    m = re.match(r"^/api/conflicts/(\w+)/resolve$", path)
    if m and method == "POST":
        try:
            services.resolve_conflict(m.group(1), body["resolution"],
                                      actor=actor_of(h, body), note=body.get("note"))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"ok": True}

    if path == "/api/merge-proposals" and method == "GET":
        return {"proposals": [dict(r) for r in db().execute(
            "SELECT * FROM merge_proposals ORDER BY created_at DESC").fetchall()]}
    if path == "/api/merge-proposals" and method == "POST":
        mid = services.propose_merge(body["source_finding_id"],
                                     body["target_finding_id"],
                                     body.get("reason"), actor=actor_of(h, body))
        return {"proposal_id": mid}
    m = re.match(r"^/api/merge-proposals/(\w+)/decide$", path)
    if m and method == "POST":
        try:
            services.decide_merge(m.group(1), bool(body.get("accept")),
                                  actor=actor_of(h, body))
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"ok": True}

    # ---- reports ----
    if path == "/api/reports" and method == "POST":
        rid = services.create_report(body["title"], body.get("period_start"),
                                     body.get("period_end"), actor=actor_of(h, body))
        return {"report_id": rid}
    if path == "/api/reports" and method == "GET":
        return {"reports": [dict(r) for r in db().execute(
            "SELECT * FROM reports ORDER BY created_at DESC").fetchall()]}
    m = re.match(r"^/api/reports/(\w+)/detail$", path)
    if m and method == "GET":
        return report_detail(m.group(1))
    m = re.match(r"^/api/reports/(\w+)/findings$", path)
    if m and method == "POST":
        services.add_report_findings(m.group(1), body["finding_ids"],
                                     actor=actor_of(h, body),
                                     replace=bool(body.get("replace", True)))
        return {"ok": True, "count": len(body["finding_ids"])}
    m = re.match(r"^/api/reports/(\w+)/suggestions$", path)
    if m and method == "POST":
        sid = services.add_suggestion(
            body["finding_id"], m.group(1), body["suggestion"],
            body.get("decision"), actor=actor_of(h, body))
        return {"suggestion_id": sid}
    m = re.match(r"^/api/reports/(\w+)/publish$", path)
    if m and method == "POST":
        services.publish_report(m.group(1), actor=actor_of(h, body))
        return {"ok": True}
    m = re.match(r"^/api/reports/(\w+)/preview$", path)
    if m and method == "POST":
        pid, manifest = services.build_preview_package(m.group(1),
                                                       actor=actor_of(h, body))
        return {"preview_id": pid, "download": "/api/preview/" + pid,
                "included": len(manifest["findings"]),
                "excluded": len(manifest["excluded_not_reviewed"])}
    m = re.match(r"^/api/preview/(\w+)$", path)
    if m and method == "GET":
        r = db().execute("SELECT object_key FROM preview_packages WHERE id=?",
                         (m.group(1),)).fetchone()
        if not r:
            raise ApiError(404, "preview_not_found")
        h._serve_object(r["object_key"], "preview.zip",
                        "application/zip")
        return None
    m = re.match(r"^/api/reports/(\w+)/export$", path)
    if m and method == "POST":
        job, ok, missing = services.export_report(m.group(1), actor=actor_of(h, body))
        return {"export_job_id": job, "ok": ok, "missing": missing}
    m = re.match(r"^/api/exports/(\w+)$", path)
    if m and method == "GET":
        r = db().execute("SELECT * FROM export_jobs WHERE id=?",
                         (m.group(1),)).fetchone()
        if not r:
            raise ApiError(404, "export_not_found")
        d = dict(r)
        d["missing_items"] = json.loads(d["missing_items"] or "[]")
        if d["object_key"] and args.get("download"):
            h._serve_object(d["object_key"], "export.zip", "application/zip")
            return None
        return d
    m = re.match(r"^/api/reports/(\w+)/review-tasks$", path)
    if m and method == "POST":
        tid = services.create_review_task(
            body["finding_id"], m.group(1), body.get("assignee"),
            body.get("note"), actor=actor_of(h, body))
        return {"task_id": tid}
    if path == "/api/review-tasks" and method == "GET":
        return {"tasks": [dict(r) for r in db().execute(
            """SELECT t.*, f.title AS finding_title FROM review_tasks t
               JOIN findings f ON f.id=t.finding_id ORDER BY t.created_at DESC"""
        ).fetchall()]}
    m = re.match(r"^/api/review-tasks/(\w+)/complete$", path)
    if m and method == "POST":
        services.complete_review_task(m.group(1), actor=actor_of(h, body))
        return {"ok": True}

    # ---- drafts ----
    if path == "/api/drafts" and method == "POST":
        did, code = services.save_draft(device_of(h, body), body.get("payload", body),
                                        body.get("resume_tokens"),
                                        body.get("share_code"))
        return {"draft_id": did, "share_code": code}
    m = re.match(r"^/api/drafts/([0-9a-zA-Z]+)$", path)
    if m and method == "GET":
        r = services.get_draft_by_code(m.group(1))
        if not r:
            raise ApiError(404, "draft_not_found")
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["resume_tokens"] = json.loads(d["resume_tokens"] or "[]")
        return d
    m = re.match(r"^/api/drafts/(\w+)/sync$", path)
    if m and method == "POST":
        services.mark_draft_synced(m.group(1))
        return {"ok": True}

    if path == "/api/timeline" and method == "GET":
        return {"timeline": services.timeline()}
    if path == "/api/workbench" and method == "GET":
        return workbench_bundle()

    raise ApiError(404, "route_not_found")


def _sid(token):
    s = uploads.get_session_by_token(token)
    if not s:
        raise ApiError(404, "session_not_found")
    return s["id"]


def create_asset(body, h):
    """
    两种工作模式都支持:
    A) 素材上传完成再录发现: 先建 asset + 分块会话, 传完 complete;
    B) 先创建带占位附件的记录: create_asset 不传 total_chunks ->
       pending_placeholder, 可立即挂到发现; 后续 start_upload 续传。
    """
    conn = db()
    aid = services.new_id("as")
    ts = now_ms()
    placeholder = body.get("placeholder", False)
    conn.execute("""INSERT INTO media_assets
        (id,kind,filename,content_type,flight_id,device_id,capture_time,
         duration_ms,width,height,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, body["kind"], body.get("filename"), body.get("content_type"),
         body.get("flight_id"), device_of(h, body), body.get("capture_time"),
         body.get("duration_ms"), body.get("width"), body.get("height"),
         "pending_placeholder" if placeholder else "uploading", ts))
    out = {"asset_id": aid}
    if not placeholder:
        if "total_chunks" not in body:
            raise ApiError(400, "total_chunks_required")
        sid, token = uploads.create_session(
            aid, body.get("filename"), int(body.get("total_size", 0)),
            int(body["total_chunks"]), body.get("whole_sha256"),
            device_of(h, body))
        out.update(session_id=sid, resume_token=token)
    conn.commit()
    services.audit(device_of(h, body) or actor_of(h, body), "asset.create",
                   "media_asset", aid,
                   {"placeholder": placeholder, "capture_time": body.get("capture_time")})
    return out


def start_upload_for_placeholder(aid, body, h):
    """模式 B: 占位附件在网络恢复后(可跨设备)开启分块上传会话。"""
    a = db().execute("SELECT * FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not a:
        raise ApiError(404, "asset_not_found")
    if a["status"] == "complete":
        raise ApiError(409, "asset_already_complete")
    open_s = db().execute(
        "SELECT * FROM upload_sessions WHERE asset_id=? AND status='open'",
        (aid,)).fetchone()
    if open_s:
        return {"asset_id": aid, "session_id": open_s["id"],
                "resume_token": open_s["resume_token"], "already_open": True}
    sid, token = uploads.create_session(
        aid, a["filename"], int(body.get("total_size", 0)),
        int(body["total_chunks"]), body.get("whole_sha256"),
        device_of(h, body))
    db().commit()
    services.audit(device_of(h, body) or actor_of(h, body),
                   "asset.resume_from_placeholder", "media_asset", aid,
                   {"total_chunks": body["total_chunks"]})
    return {"asset_id": aid, "session_id": sid, "resume_token": token}


def put_chunk(token, index, h):
    sid = _sid(token)
    n = int(h.headers.get("Content-Length") or 0)
    data = h.rfile.read(n)
    code, payload = uploads.put_chunk(sid, index, data, device_of(h))
    if code not in (200, 201):
        raise ApiError(code, payload.get("error", "chunk_error"), payload)
    return payload


def session_status(token):
    s = uploads.get_session_by_token(token)
    if not s:
        raise ApiError(404, "session_not_found")
    return {"session": dict(s), "received_chunks": uploads.received_chunks(s["id"])}


def report_detail(rid):
    r = db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ApiError(404, "report_not_found")
    d = dict(r)
    d["finding_ids"] = [x["finding_id"] for x in db().execute(
        "SELECT finding_id FROM report_findings WHERE report_id=?", (rid,)).fetchall()]
    d["suggestions"] = [dict(x) for x in db().execute(
        "SELECT * FROM preview_suggestions WHERE report_id=? ORDER BY created_at",
        (rid,)).fetchall()]
    return d


def list_plots():
    out = []
    for prow in db().execute("SELECT * FROM plots ORDER BY created_at").fetchall():
        d = dict(prow)
        v = db().execute(
            "SELECT id,version,geometry,source,change_note,created_at FROM plot_versions"
            " WHERE plot_id=? ORDER BY version DESC", (prow["id"],)).fetchone()
        d["latest_version"] = v["version"]
        d["geometry"] = json.loads(v["geometry"])
        d["versions"] = [{"version": r["version"], "source": r["source"],
                          "change_note": r["change_note"], "created_at": r["created_at"]}
                         for r in db().execute(
                             "SELECT version,source,change_note,created_at FROM plot_versions"
                             " WHERE plot_id=? ORDER BY version", (prow["id"],)).fetchall()]
        out.append(d)
    return out


def get_plot(pid):
    p = db().execute("SELECT * FROM plots WHERE id=?", (pid,)).fetchone()
    if not p:
        raise ApiError(404, "plot_not_found")
    d = dict(p)
    d["versions"] = []
    for r in db().execute(
            "SELECT * FROM plot_versions WHERE plot_id=? ORDER BY version",
            (pid,)).fetchall():
        vd = dict(r)
        vd["geometry"] = json.loads(vd["geometry"])
        d["versions"].append(vd)
    d["findings"] = [dict(r) for r in db().execute(
        "SELECT id,title,status,attribution_status,geo_error_m FROM findings WHERE plot_id=?",
        (pid,)).fetchall()]
    return d


def get_asset_detail(aid):
    a = db().execute("SELECT * FROM media_assets WHERE id=?", (aid,)).fetchone()
    if not a:
        raise ApiError(404, "asset_not_found")
    d = dict(a)
    d["sessions"] = [dict(r) for r in db().execute(
        "SELECT id,resume_token,status,total_chunks,created_at,completed_at"
        " FROM upload_sessions WHERE asset_id=?", (aid,)).fetchall()]
    d["segments"] = [dict(r) for r in db().execute(
        "SELECT * FROM video_segments WHERE asset_id=? ORDER BY source_start_ms",
        (aid,)).fetchall()]
    d["transcriptions"] = [dict(r) for r in db().execute(
        "SELECT * FROM transcriptions WHERE asset_id=? ORDER BY ordered_at",
        (aid,)).fetchall()]
    return d


def list_findings():
    out = []
    for f in db().execute("SELECT * FROM findings ORDER BY created_at DESC").fetchall():
        d = dict(f)
        d["evidence_count"] = db().execute(
            "SELECT COUNT(*) c FROM evidences WHERE finding_id=?", (f["id"],)).fetchone()["c"]
        d["event_ids"] = [r["event_id"] for r in db().execute(
            "SELECT DISTINCT event_id FROM evidences WHERE finding_id=? AND event_id IS NOT NULL",
            (f["id"],)).fetchall()]
        out.append(d)
    return out


def finding_detail(fid):
    f = db().execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not f:
        raise ApiError(404, "finding_not_found")
    d = dict(f)
    evs = []
    for e in db().execute("SELECT * FROM evidences WHERE finding_id=?",
                          (fid,)).fetchall():
        ed = dict(e)
        a = db().execute("SELECT id,kind,filename,status,sha256,capture_time,upload_time"
                         " FROM media_assets WHERE id=?", (e["asset_id"],)).fetchone()
        ed["asset"] = dict(a) if a else None
        evs.append(ed)
    d["evidences"] = evs
    d["attribution_history"] = [dict(r) for r in db().execute(
        "SELECT * FROM attribution_history WHERE finding_id=? ORDER BY created_at",
        (fid,)).fetchall()]
    return d


def workbench_bundle():
    return {
        "plots": list_plots(),
        "flights": [dict(r) for r in db().execute(
            "SELECT * FROM flights ORDER BY flight_date").fetchall()],
        "events": [dict(r) for r in db().execute(
            "SELECT * FROM inspection_events ORDER BY event_date").fetchall()],
        "assets": [dict(r) for r in db().execute(
            """SELECT a.* FROM media_assets a ORDER BY a.created_at""").fetchall()],
        "findings": list_findings(),
        "segments": [dict(r) for r in db().execute(
            "SELECT * FROM video_segments ORDER BY source_start_ms").fetchall()],
        "timeline": services.timeline(),
    }


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_object(self, key, download_name, ctype):
        path = storage.get_path(key)
        self._serve_file(path, ctype, download_name=download_name)

    def _serve_file(self, path, ctype=None, download_name=None):
        try:
            fsize = os.path.getsize(path)
        except OSError:
            self.send_error(404)
            return
        start, end = 0, fsize - 1
        rng = self.headers.get("Range")
        status = 200
        if rng:
            mm = re.match(r"bytes=(\d+)-(\d*)", rng)
            if mm:
                start = int(mm.group(1))
                if mm.group(2):
                    end = min(int(mm.group(2)), fsize - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0]
                         or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
        if download_name:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % download_name)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                buf = f.read(min(1 << 20, remaining))
                if not buf:
                    break
                self.wfile.write(buf)
                remaining -= len(buf)

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        # /media/<sha256> -> 对象存储
        mm = re.match(r"^/media/([0-9a-f]{64})$", path)
        if mm:
            row = db().execute(
                "SELECT kind, content_type, filename FROM media_assets WHERE sha256=?",
                (mm.group(1),)).fetchone()
            ctype = (row["content_type"] if row and row["content_type"] else
                     {"image": "image/jpeg", "video": "video/mp4",
                      "audio": "audio/mp4"}.get(row["kind"] if row else None,
                                               "application/octet-stream"))
            self._serve_object(mm.group(1), None, ctype)
            return
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WEB_ROOT, rel)
        if not full.startswith(WEB_ROOT + os.sep) or not os.path.isfile(full):
            self.send_error(404)
            return
        self._serve_file(full)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        args = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            self._serve_static(path)
            return
        try:
            ctype = self.headers.get("Content-Type") or ""
            body = jbody(self) if (method in ("POST", "PUT", "PATCH")
                                   and "application/json" in ctype) else {}
            result = api_dispatch(method, path, args, body, self)
            if result is None:
                return  # 文件流响应已直接发出
            self._send_json(result)
        except ApiError as e:
            payload = {"error": e.code}
            payload.update(e.extra)
            self._send_json(payload, e.status)
        except Exception as e:
            self._send_json({"error": "internal", "detail": str(e)}, 500)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    init()
    storage.init()
    import seed
    seed.seed_if_empty()
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("航拍农田报告工作台: http://localhost:%d" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
