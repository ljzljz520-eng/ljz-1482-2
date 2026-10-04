# -*- coding: utf-8 -*-
"""
业务服务层: 发现/证据、多人标注、边界修订与归属重判、复核事项、
报告/预览包/导出、语音转写晚到、视频裁切时间码、合并建议、时间线。

关键业务规则:
- 发现状态机 needs_evidence -> pending_review -> reviewed (可 disputed)。
- 同一发现允许挂多次巡检事件的证据; 系统绝不按图像相似度自动合并。
- 地块修订只新增版本, 旧版本不可变; 自动归属随新版本重判并留痕;
  已派出复核事项保留 original_report_id 引用不变。
- 预览包只包含 reviewed 发现 + 用户填写的建议文本; 不输出任何系统自动
  生成的农事处置。
"""
import json
import os
import time
import uuid
import zipfile

import geo
import storage
import uploads
from db import db, now_ms

NEAR_M = 30.0  # "同处"标注的初步距离阈值


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


def audit(actor, action, etype, eid, detail=None):
    db().execute(
        "INSERT INTO audit_log (actor, action, entity_type, entity_id, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor, action, etype, eid, json.dumps(detail, ensure_ascii=False)
         if detail is not None else None, now_ms()))
    db().commit()


def rowdict(r):
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- devices
def register_device(name, platform=None):
    conn = db()
    did = new_id("dev")
    conn.execute("INSERT INTO devices (device_id, name, platform, registered_at)"
                 " VALUES (?,?,?,?)", (did, name, platform, now_ms()))
    conn.commit()
    audit(name, "device.register", "device", did)
    return did


# ---------------------------------------------------------------- plots
def create_plot(name, crop, geometry, source="import", note=None, actor="system"):
    conn = db()
    pid = new_id("plot")
    ts = now_ms()
    conn.execute("INSERT INTO plots (id,name,crop,current_version,created_at,updated_at)"
                 " VALUES (?,?,?,1,?,?)", (pid, name, crop, ts, ts))
    vid = new_id("pv")
    conn.execute("""INSERT INTO plot_versions
        (id,plot_id,version,geometry,source,change_note,edited_by,created_at)
        VALUES (?,?,1,?,?,?,?,?)""",
        (vid, pid, json.dumps(geometry), source, note, actor, ts))
    conn.commit()
    audit(actor, "plot.create", "plot", pid, {"source": source})
    return pid


def revise_plot(pid, geometry, note, actor="system"):
    """边界修订: 新增不可变版本, current_version+1, 并触发发现归属重判。"""
    conn = db()
    plot = conn.execute("SELECT * FROM plots WHERE id=?", (pid,)).fetchone()
    if not plot:
        raise ValueError("plot_not_found")
    new_version = plot["current_version"] + 1
    ts = now_ms()
    conn.execute("""INSERT INTO plot_versions
        (id,plot_id,version,geometry,source,change_note,edited_by,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (new_id("pv"), pid, new_version, json.dumps(geometry), "edit", note,
         actor, ts))
    conn.execute("UPDATE plots SET current_version=?, updated_at=? WHERE id=?",
                 (new_version, ts, pid))
    conn.commit()
    audit(actor, "plot.revise", "plot", pid, {"new_version": new_version})
    reattribute_after_revision(pid, new_version, actor, reason=note)
    return new_version


def latest_geometry(pid, version=None):
    conn = db()
    if version:
        r = conn.execute("SELECT geometry FROM plot_versions WHERE plot_id=? AND version=?",
                         (pid, version)).fetchone()
    else:
        r = conn.execute(
            "SELECT geometry FROM plot_versions WHERE plot_id=? "
            "ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
    return r["geometry"] if r else None


def reattribute_after_revision(pid, new_version, actor, reason=None):
    """边界修订后重新判断发现归属 (仅自动归属; 人工归属锁定不动)。"""
    conn = db()
    geom = latest_geometry(pid, new_version)
    # 所有带地理坐标的发现都重新判断 (新边界可能覆盖原属其他地块/无归属的发现)
    rows = conn.execute("SELECT * FROM findings WHERE lat IS NOT NULL").fetchall()
    for f in rows:
        old_status = f["attribution_status"]
        if old_status == "manual":
            continue  # 人工锁定的归属不自动改
        st, edge = geo.attribute_with_error(f["lon"], f["lat"], geom,
                                            f["geo_error_m"] or 0)
        new_plot = pid if st in ("auto_inside", "auto_buffer") else None
        # 若不在本地块, 仍可能在其他地块: 对所有地块逐一判断
        if new_plot is None:
            new_plot, st = _attribute_among_plots(f["lon"], f["lat"],
                                                  f["geo_error_m"] or 0,
                                                  exclude=pid)
        if new_plot != f["plot_id"] or st != old_status:
            conn.execute("""INSERT INTO attribution_history
                (id,finding_id,old_plot_id,new_plot_id,old_version,new_version,
                 old_status,new_status,reason,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (new_id("ah"), f["id"], f["plot_id"], new_plot,
                 f["plot_version"], new_version, old_status, st, reason, now_ms()))
            conn.execute("""UPDATE findings SET plot_id=?, attribution_status=?,
                plot_version=?, updated_at=? WHERE id=?""",
                (new_plot, st, new_version if new_plot == pid else
                 _current_version(new_plot), now_ms(), f["id"]))
            audit(actor, "finding.reattribute", "finding", f["id"],
                  {"old_plot": f["plot_id"], "new_plot": new_plot,
                   "old_status": old_status, "new_status": st})
    conn.commit()


def _current_version(pid):
    if not pid:
        return None
    r = db().execute("SELECT current_version FROM plots WHERE id=?", (pid,)).fetchone()
    return r["current_version"] if r else None


def _attribute_among_plots(lon, lat, error_m, exclude=None):
    """在所有地块最新版本中找归属; 多个命中时优先 inside, 仍多义返回 (None,'none')由人工定。"""
    conn = db()
    plots = conn.execute("SELECT id, current_version FROM plots").fetchall()
    inside, buffer_hits = [], []
    for p in plots:
        geom = latest_geometry(p["id"], p["current_version"])
        st, edge = geo.attribute_with_error(lon, lat, geom, error_m)
        if st == "auto_inside":
            inside.append((p["id"], st))
        elif st == "auto_buffer":
            buffer_hits.append((p["id"], st))
    if len(inside) == 1:
        return inside[0]
    if len(inside) > 1:
        return None, "none"  # 重叠几何, 必须人工
    if len(buffer_hits) == 1:
        return buffer_hits[0]
    return None, "none"


# ---------------------------------------------------------------- flights/events
def create_flight(flight_date, pilot=None, aircraft=None, note=None, actor="mobile"):
    fid = new_id("fl")
    db().execute("INSERT INTO flights (id,flight_date,pilot,aircraft,note,created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 (fid, flight_date, pilot, aircraft, note, now_ms()))
    db().commit()
    audit(actor, "flight.create", "flight", fid, {"flight_date": flight_date})
    return fid


def create_event(flight_id, event_date, kind="aerial", inspector=None, note=None,
                 actor="mobile"):
    eid = new_id("ev")
    db().execute("""INSERT INTO inspection_events
        (id,flight_id,event_date,kind,inspector,note,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (eid, flight_id, event_date, kind, inspector, note, now_ms()))
    db().commit()
    audit(actor, "inspection_event.create", "inspection_event", eid,
          {"flight_id": flight_id, "date": event_date, "kind": kind})
    return eid


# ---------------------------------------------------------------- findings
def _resolve_position(payload):
    """
    返回 (lon, lat, method, error_m, geo_note)。
    若给 raw camera (frame 参数) 则走 frame_projection 合法通道。
    严禁把 pixel_x/pixel_y 直接当地理坐标。
    """
    lon = payload.get("lon")
    lat = payload.get("lat")
    method = payload.get("geo_method")
    err = payload.get("geo_error_m")
    note = payload.get("geo_note")
    cam = payload.get("camera")
    if cam and (lon is None or payload.get("force_frame_projection")):
        lon, lat, err = geo.frame_projection(
            cam["drone_lon"], cam["drone_lat"], cam["drone_alt_m"],
            cam.get("yaw_deg", 0), cam.get("pitch_deg", -90),
            cam.get("roll_deg", 0),
            cam["focal_px"], cam["frame_w"], cam["frame_h"],
            cam["px"], cam["py"],
            gnss_error_m=cam.get("gnss_error_m", 3.0),
            attitude_error_deg=cam.get("attitude_error_deg", 2.0),
            terrain_height_uncertainty_m=cam.get("terrain_uncertainty_m", 5.0))
        method = "frame_projection"
        note = note or ("frame_projection H=%sm yaw=%s pitch=%s; 像素(%s,%s)"
                        % (cam["drone_alt_m"], cam.get("yaw_deg", 0),
                           cam.get("pitch_deg", -90), cam["px"], cam["py"]))
    geo.validate_geo(method, lon, lat, err if (lon is not None) else None)
    return lon, lat, method, err, note


def create_finding(payload, actor="mobile"):
    conn = db()
    lon, lat, method, err, gnote = _resolve_position(payload)
    plot_id, attr_status = None, None
    plot_version = None
    if lon is not None:
        plot_id, attr_status = _attribute_among_plots(lon, lat, err)
        plot_version = _current_version(plot_id)
        if attr_status == "auto_buffer":
            gnote = (gnote or "") + " [边界在定位误差内, 需人工确认归属]"
    fid = new_id("fd")
    ts = now_ms()
    conn.execute("""INSERT INTO findings
        (id,title,category,severity,status,lon,lat,geo_method,geo_error_m,geo_note,
         plot_id,attribution_status,plot_version,created_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, payload["title"], payload.get("category"), payload.get("severity"),
         "needs_evidence", lon, lat, method, err, gnote, plot_id, attr_status,
         plot_version, actor, ts, ts))
    conn.commit()
    audit(actor, "finding.create", "finding", fid,
          {"geo_method": method, "plot": plot_id, "attr": attr_status})
    # 可选随创建直接挂第一条证据
    for ev in payload.get("evidences", []):
        add_evidence(fid, ev, actor)
    return fid


def add_evidence(fid, ev, actor="mobile"):
    conn = db()
    f = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not f:
        raise ValueError("finding_not_found")
    asset_id = ev.get("asset_id")
    if asset_id:
        a = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        if not a:
            raise ValueError("asset_not_found")
        if a["status"] != "complete":
            # 允许挂占位附件, 但发现保持待补证据; 状态在素材完成时推进
            pass
    # pixel_x/pixel_y 是画面像素, 仅与证据一同保存, 不参与地理计算
    eid = new_id("evd")
    conn.execute("""INSERT INTO evidences
        (id,finding_id,event_id,asset_id,segment_id,pixel_x,pixel_y,
         frame_width,frame_height,note,created_by,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, fid, ev.get("event_id"), asset_id, ev.get("segment_id"),
         ev.get("pixel_x"), ev.get("pixel_y"), ev.get("frame_width"),
         ev.get("frame_height"), ev.get("note"), actor, now_ms()))
    conn.commit()
    audit(actor, "evidence.add", "evidence", eid,
          {"finding": fid, "asset": asset_id, "event": ev.get("event_id")})
    _refresh_status(fid)
    return eid


def _refresh_status(fid):
    """根据证据完整度推进状态机; reviewed 不回退。"""
    conn = db()
    f = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if f["status"] in ("reviewed", "disputed"):
        return
    evs = conn.execute("SELECT * FROM evidences WHERE finding_id=?", (fid,)).fetchall()
    complete = any(e["asset_id"] and conn.execute(
        "SELECT status FROM media_assets WHERE id=?",
        (e["asset_id"],)).fetchone()["status"] == "complete" for e in evs)
    target = "pending_review" if (evs and complete) else "needs_evidence"
    if target != f["status"]:
        conn.execute("UPDATE findings SET status=?, updated_at=? WHERE id=?",
                     (target, now_ms(), fid))
        conn.commit()
        audit(f["created_by"], "finding.status", "finding", fid,
              {"status": target})


def refresh_findings_for_asset(asset_id):
    """上传完成/转写晚到后, 推进引用该素材的发现状态。"""
    conn = db()
    ids = [r["finding_id"] for r in conn.execute(
        "SELECT DISTINCT finding_id FROM evidences WHERE asset_id=?",
        (asset_id,)).fetchall()]
    for fid in ids:
        _refresh_status(fid)


def set_finding_status(fid, status, actor="reviewer", note=None):
    allowed = {"pending_review", "reviewed", "disputed"}
    if status not in allowed:
        raise ValueError("bad_status")
    conn = db()
    if status == "reviewed":
        # 审核必须保证有完整素材证据
        ev = conn.execute(
            """SELECT e.id FROM evidences e JOIN media_assets a ON a.id=e.asset_id
               WHERE e.finding_id=? AND a.status='complete' LIMIT 1""",
            (fid,)).fetchone()
        if not ev:
            raise ValueError("cannot_review_without_complete_evidence")
    conn.execute("UPDATE findings SET status=?, updated_at=? WHERE id=?",
                 (status, now_ms(), fid))
    conn.commit()
    audit(actor, "finding.review", "finding", fid, {"status": status, "note": note})


def manual_set_plot(fid, plot_id, actor="reviewer", note=None):
    conn = db()
    conn.execute("""UPDATE findings SET plot_id=?, attribution_status='manual',
        plot_version=?, updated_at=? WHERE id=?""",
        (plot_id, _current_version(plot_id), now_ms(), fid))
    conn.commit()
    audit(actor, "finding.manual_plot", "finding", fid,
          {"plot": plot_id, "note": note})


# ---------------------------------------------------------------- annotations
def add_annotation(payload, actor):
    """两人标注同处: 保存每个人的标注, 计算 same_spot 冲突, 不覆盖、不自动合并。"""
    conn = db()
    lon, lat = payload["lon"], payload["lat"]
    method = payload.get("geo_method")
    geo.validate_geo(method, lon, lat, payload.get("geo_error_m"))
    aid = new_id("an")
    conn.execute("""INSERT INTO annotations
        (id,finding_id,event_id,author,lon,lat,geo_method,geo_error_m,label,
         comment,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, payload.get("finding_id"), payload.get("event_id"), actor,
         lon, lat, method, payload.get("geo_error_m"), payload.get("label"),
         payload.get("comment"), now_ms()))
    conn.commit()
    # 找同事件(或无事件)下他人的近距离标注
    q = "SELECT * FROM annotations WHERE id != ?"
    args = [aid]
    if payload.get("event_id"):
        q += " AND event_id = ?"
        args.append(payload["event_id"])
    conflicts_created = []
    for other in conn.execute(q, tuple(args)).fetchall():
        d = geo.distance_m(lon, lat, other["lon"], other["lat"])
        if d <= NEAR_M and other["author"] != actor:
            # 同一对已有 open 冲突则不重复建
            dup = conn.execute(
                """SELECT id FROM conflicts WHERE kind='same_spot' AND status='open'
                   AND ((ref_a=? AND ref_b=?) OR (ref_a=? AND ref_b=?))""",
                (aid, other["id"], other["id"], aid)).fetchone()
            if dup:
                continue
            cid = new_id("cf")
            conn.execute("""INSERT INTO conflicts
                (id,kind,ref_a,ref_b,distance_m,status,created_at)
                VALUES (?, 'same_spot', ?, ?, ?, 'open', ?)""",
                (cid, aid, other["id"], round(d, 2), now_ms()))
            conflicts_created.append(cid)
    conn.commit()
    audit(actor, "annotation.add", "annotation", aid,
          {"conflicts": conflicts_created})
    return aid, conflicts_created


def resolve_conflict(cid, resolution, actor, note=None):
    """resolution: keep_both | merged(target_finding) | dismissed。"""
    conn = db()
    c = conn.execute("SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("conflict_not_found")
    status_map = {"keep_both": "resolved_keep_both",
                  "merged": "resolved_merged",
                  "dismissed": "resolved_dismissed"}
    if resolution not in status_map:
        raise ValueError("bad_resolution")
    conn.execute("""UPDATE conflicts SET status=?, resolution_note=?, resolved_by=?,
        resolved_at=? WHERE id=?""",
        (status_map[resolution], note, actor, now_ms(), cid))
    conn.commit()
    audit(actor, "conflict.resolve", "conflict", cid,
          {"resolution": resolution, "note": note})


# ---------------------------------------------------------------- merge proposals
def propose_merge(source_id, target_id, reason, actor):
    """人工合并建议; 系统没有任何按图像相似度自动合并的路径。"""
    conn = db()
    mid = new_id("mp")
    conn.execute("""INSERT INTO merge_proposals
        (id,source_finding_id,target_finding_id,reason,created_by,status,created_at)
        VALUES (?,?,?,?,?, 'proposed', ?)""",
        (mid, source_id, target_id, reason, actor, now_ms()))
    conn.commit()
    audit(actor, "merge.propose", "merge_proposal", mid,
        {"source": source_id, "target": target_id, "reason": reason})
    return mid


def decide_merge(mid, accept, actor):
    conn = db()
    m = conn.execute("SELECT * FROM merge_proposals WHERE id=?", (mid,)).fetchone()
    if not m or m["status"] != "proposed":
        raise ValueError("proposal_not_open")
    ts = now_ms()
    if accept:
        # 合并 = 证据迁移 + 源发现标记 disputed (保留审计可追溯, 不物理删除)
        conn.execute("UPDATE evidences SET finding_id=? WHERE finding_id=?",
                     (m["target_finding_id"], m["source_finding_id"]))
        conn.execute("UPDATE findings SET status='disputed', updated_at=? WHERE id=?",
                     (ts, m["source_finding_id"]))
        conn.execute(
            "UPDATE merge_proposals SET status='accepted', decided_at=? WHERE id=?",
            (ts, mid))
        audit(actor, "merge.accept", "merge_proposal", mid, None)
    else:
        conn.execute(
            "UPDATE merge_proposals SET status='rejected', decided_at=? WHERE id=?",
            (ts, mid))
        audit(actor, "merge.reject", "merge_proposal", mid, None)
    conn.commit()


# ---------------------------------------------------------------- video/transcript
def add_video_segment(asset_id, source_start_ms, source_end_ms, name=None,
                      source_segment_id=None):
    """
    视频裁切产生新时间码: t_new = t_source - source_start_ms。
    源素材不修改, 只记录 offset, 所有引用可回溯。
    """
    conn = db()
    a = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    if not a:
        raise ValueError("asset_not_found")
    if source_end_ms <= source_start_ms:
        raise ValueError("bad_time_range")
    if a["duration_ms"] and source_end_ms > a["duration_ms"]:
        raise ValueError("segment_exceeds_source")
    sid = new_id("seg")
    conn.execute("""INSERT INTO video_segments
        (id,asset_id,source_segment_id,source_start_ms,source_end_ms,
         timecode_offset_ms,name,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (sid, asset_id, source_segment_id, source_start_ms, source_end_ms,
         source_start_ms, name, now_ms()))
    conn.commit()
    audit("system", "video.crop", "video_segment", sid,
          {"asset": asset_id, "offset": source_start_ms})
    return sid


def remap_timecode(segment_id, source_time_ms):
    """给定源素材时间码, 返回裁切后的时间码。"""
    r = db().execute("SELECT * FROM video_segments WHERE id=?",
                     (segment_id,)).fetchone()
    if not r:
        raise ValueError("segment_not_found")
    if not (r["source_start_ms"] <= source_time_ms <= r["source_end_ms"]):
        raise ValueError("time_outside_segment")
    return source_time_ms - r["timecode_offset_ms"]


def order_transcription(asset_id):
    conn = db()
    tid = new_id("tx")
    conn.execute("""INSERT INTO transcriptions (id,asset_id,status,ordered_at)
        VALUES (?,?,'pending',?)""", (tid, asset_id, now_ms()))
    conn.commit()
    audit("system", "transcription.order", "transcription", tid, {"asset": asset_id})
    return tid


def fulfill_transcription(asset_id, text, provider="mock-asr", failed=False):
    """语音转写晚到: 素材早就 complete, 转写结果异步到达。"""
    conn = db()
    t = conn.execute(
        "SELECT * FROM transcriptions WHERE asset_id=? ORDER BY ordered_at DESC LIMIT 1",
        (asset_id,)).fetchone()
    if not t:
        t_id = order_transcription(asset_id)
        t = conn.execute("SELECT * FROM transcriptions WHERE id=?", (t_id,)).fetchone()
    ts = now_ms()
    conn.execute("UPDATE transcriptions SET text=?, provider=?, status=?, ready_at=?"
                 " WHERE id=?",
                 (text, provider, "failed" if failed else "ready",
                  None if failed else ts, t["id"]))
    conn.commit()
    audit("system", "transcription.ready", "transcription", t["id"],
          {"asset": asset_id, "late_ms": ts - t["ordered_at"]})
    refresh_findings_for_asset(asset_id)


# ---------------------------------------------------------------- review tasks
def create_review_task(finding_id, report_id, assignee, note=None, actor="manager"):
    """已派出复核事项; original_report_id 永久保留, 不随后续重判修改。"""
    tid = new_id("rt")
    db().execute("""INSERT INTO review_tasks
        (id,finding_id,original_report_id,assignee,note,status,created_at)
        VALUES (?,?,?,?,?, 'assigned', ?)""",
        (tid, finding_id, report_id, assignee, note, now_ms()))
    db().commit()
    audit(actor, "review_task.create", "review_task", tid,
          {"finding": finding_id, "original_report": report_id})
    return tid


def complete_review_task(tid, actor="manager"):
    db().execute("UPDATE review_tasks SET status='done', completed_at=? WHERE id=?",
                 (now_ms(), tid))
    db().commit()
    audit(actor, "review_task.done", "review_task", tid, None)


# ---------------------------------------------------------------- reports
def create_report(title, period_start=None, period_end=None, actor="manager"):
    rid = new_id("rp")
    db().execute("""INSERT INTO reports (id,title,period_start,period_end,status,
        created_by,created_at) VALUES (?,?,?,?, 'draft', ?,?)""",
        (rid, title, period_start, period_end, actor, now_ms()))
    db().commit()
    audit(actor, "report.create", "report", rid, None)
    return rid


def add_report_findings(rid, finding_ids, actor="manager", replace=False):
    conn = db()
    if replace:
        conn.execute("DELETE FROM report_findings WHERE report_id=?", (rid,))
    for fid in finding_ids:
        conn.execute("INSERT OR IGNORE INTO report_findings (report_id,finding_id)"
                     " VALUES (?,?)", (rid, fid))
    conn.commit()
    audit(actor, "report.add_findings", "report", rid,
          {"count": len(finding_ids), "replace": replace})


def add_suggestion(finding_id, report_id, suggestion, decision=None, actor="agronomist"):
    """建议/处置决定都由用户填写; 系统不自动生成。"""
    sid = new_id("sg")
    db().execute("""INSERT INTO preview_suggestions
        (id,finding_id,report_id,suggestion,decision,created_by,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (sid, finding_id, report_id, suggestion, decision, actor, now_ms()))
    db().commit()
    audit(actor, "suggestion.add", "preview_suggestion", sid, {
        "finding": finding_id, "report": report_id})
    return sid


def publish_report(rid, actor="manager"):
    conn = db()
    conn.execute("UPDATE reports SET status='published', published_at=? WHERE id=?",
                 (now_ms(), rid))
    conn.commit()
    audit(actor, "report.publish", "report", rid, None)


def build_preview_package(rid, actor="manager"):
    """
    预览包铁律: 只包含 reviewed 发现 + 用户填写的建议文本。
    不含系统自动农事处置; 未审核发现一律不进包。
    """
    conn = db()
    r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ValueError("report_not_found")
    rows = conn.execute("""
        SELECT f.* FROM report_findings rf JOIN findings f ON f.id=rf.finding_id
        WHERE rf.report_id=? ORDER BY f.updated_at DESC""", (rid,)).fetchall()
    reviewed, excluded = [], []
    for f in rows:
        (reviewed if f["status"] == "reviewed" else excluded).append(
            {"id": f["id"], "title": f["title"], "status": f["status"]})
    suggestions = [dict(s) for s in conn.execute(
        "SELECT * FROM preview_suggestions WHERE report_id=?", (rid,)).fetchall()]
    evidence = []
    for f in reviewed:
        for e in conn.execute("""
            SELECT e.*, a.sha256, a.kind, a.filename, a.status AS asset_status
            FROM evidences e LEFT JOIN media_assets a ON a.id=e.asset_id
            WHERE e.finding_id=?""", (f["id"],)).fetchall():
            if e["asset_id"] and e["asset_status"] == "complete":
                evidence.append({"finding_id": f["id"], "asset_id": e["asset_id"],
                                 "sha256": e["sha256"], "kind": e["kind"],
                                 "filename": e["filename"]})
    manifest = {
        "report_id": rid, "title": r["title"],
        "built_at": now_ms(),
        "rule": "only_reviewed_findings_and_user_suggestions; no automated decisions",
        "findings": reviewed,
        "excluded_not_reviewed": excluded,
        "user_suggestions": suggestions,
        "evidence": evidence,
    }
    name = "preview-%s.zip" % rid
    path = storage.derived_path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("preview.html", _render_preview_html(manifest))
        z.writestr("suggestions.md", _render_suggestions_md(manifest))
        z.writestr("README.txt",
                   "本预览包仅包含已审核发现与用户填写的建议, 不构成农事处置决定。\n"
                   "未审核发现: %d 项已排除。\n" % len(excluded))
        seen = set()
        for ev in evidence:
            if ev["sha256"] in seen:
                continue
            seen.add(ev["sha256"])
            try:
                z.write(storage.get_path(ev["sha256"]),
                        "media/" + (ev["filename"] or ev["asset_id"]))
            except FileNotFoundError:
                pass  # 预览容忍缺失; 完整导出才报错
    key = None
    with open(path, "rb") as fh:
        key = storage.put_bytes(fh.read())
    pid = new_id("pp")
    conn.execute("""INSERT INTO preview_packages
        (id,report_id,object_key,manifest,only_reviewed,created_at)
        VALUES (?,?,?,?,'1',?)""",
        (pid, rid, key, json.dumps(manifest, ensure_ascii=False), now_ms()))
    conn.commit()
    audit(actor, "preview.build", "preview_package", pid,
          {"included": len(reviewed), "excluded": len(excluded)})
    return pid, manifest


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_preview_html(m):
    items = []
    for f in m["findings"]:
        sugs = [s for s in m["user_suggestions"] if s["finding_id"] == f["id"]]
        sug_html = "".join(
            "<li>%s <em>(%s%s)</em></li>" % (
                _esc(s["suggestion"]), _esc(s.get("created_by") or "用户"),
                ("；决定: " + _esc(s["decision"])) if s.get("decision") else "；决定: 未填写")
            for s in sugs) or "<li>（暂无用户建议）</li>"
        items.append(
            "<section><h3>%s</h3><p>类别:%s 严重度:%s 定位:%s ±%sm</p>"
            "<ul class='sugg'>%s</ul></section>" % (
                _esc(f["title"]), _esc(f.get("category")), _esc(f.get("severity")),
                _esc(f.get("geo_method")), f.get("geo_error_m") or 0, sug_html))
    return ("<!doctype html><meta charset=utf-8><title>预览 %s</title>"
            "<h1>%s（预览）</h1><div class=note>本预览仅含已审核发现与用户填写的建议，"
            "不自动决定农事处置。</div>%s" % (
                _esc(m["title"]), _esc(m["title"]), "".join(items)))


def _render_suggestions_md(m):
    out = ["# 用户填写的建议（预览 %s）\n" % m["title"],
           "> 系统不自动生成农事处置；以下均为用户手工填写。\n"]
    for s in m["user_suggestions"]:
        out.append("- 发现 %s: %s | 决定: %s（%s）" % (
            s["finding_id"], s["suggestion"], s.get("decision") or "未决定",
            s.get("created_by") or "用户"))
    return "\n".join(out)


def export_report(rid, actor="manager"):
    """
    完整导出: 校验每个引用素材在对象存储中存在;
    缺失则记录 missing_items, 作业失败并在网页待补状态可见。
    """
    conn = db()
    job = new_id("ej")
    conn.execute("""INSERT INTO export_jobs (id,report_id,status,created_at)
        VALUES (?,?,'requested',?)""", (job, rid, now_ms()))
    conn.commit()
    refs = conn.execute("""
        SELECT e.id AS evidence_id, e.asset_id, a.sha256, a.filename, a.status,
               f.title
        FROM report_findings rf
        JOIN evidences e ON e.finding_id=rf.finding_id
        JOIN media_assets a ON a.id=e.asset_id
        JOIN findings f ON f.id=e.finding_id
        WHERE rf.report_id=?""", (rid,)).fetchall()
    missing = []
    for r in refs:
        if r["status"] != "complete" or not r["sha256"] or not storage.exists(r["sha256"]):
            missing.append({"evidence_id": r["evidence_id"],
                            "asset_id": r["asset_id"],
                            "filename": r["filename"],
                            "finding": r["title"],
                            "reason": ("asset_status=%s" % r["status"]
                                       if r["status"] != "complete" else "object_lost")})
    ts = now_ms()
    if missing:
        conn.execute("""UPDATE export_jobs SET status='failed', missing_items=?,
            log=?, completed_at=? WHERE id=?""",
            (json.dumps(missing, ensure_ascii=False),
             "导出中止: %d 个素材缺失" % len(missing), ts, job))
        conn.commit()
        audit(actor, "export.fail_missing_media", "export_job", job,
              {"missing": len(missing)})
        return job, False, missing

    path = storage.derived_path("export-%s.zip" % rid)
    manifest = {"report_id": rid, "exported_at": ts,
                "files": [dict(r) for r in refs]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for r in refs:
            z.write(storage.get_path(r["sha256"]),
                    "media/" + (r["filename"] or r["asset_id"]))
    with open(path, "rb") as fh:
        key = storage.put_bytes(fh.read())
    conn.execute("""UPDATE export_jobs SET status='complete', object_key=?,
        missing_items='[]', completed_at=? WHERE id=?""", (key, ts, job))
    conn.commit()
    audit(actor, "export.complete", "export_job", job, {"files": len(refs)})
    return job, True, []


# ---------------------------------------------------------------- drafts (offline/cross-device)
def save_draft(device_id, payload, resume_tokens=None, share_code=None):
    conn = db()
    did = new_id("dr")
    code = share_code or uuid.uuid4().hex[:8]
    conn.execute("""INSERT INTO client_drafts
        (id,device_id,payload,resume_tokens,share_code,status,created_at)
        VALUES (?,?,?,?,?, 'open', ?)""",
        (did, device_id, json.dumps(payload, ensure_ascii=False),
         json.dumps(resume_tokens or []), code, now_ms()))
    conn.commit()
    return did, code


def get_draft_by_code(code):
    return db().execute("SELECT * FROM client_drafts WHERE share_code=?",
                        (code,)).fetchone()


def mark_draft_synced(did):
    db().execute("UPDATE client_drafts SET status='synced', synced_at=? WHERE id=?",
                 (now_ms(), did))
    db().commit()


# ---------------------------------------------------------------- timeline
def timeline():
    """
    统一时间线: 飞行日期 / 拍摄时刻 / 上传时刻 三类事件分轨展示,
    绝不混用。
    """
    conn = db()
    out = []
    for f in conn.execute("SELECT id, flight_date, pilot, aircraft FROM flights").fetchall():
        out.append({"track": "flight_date", "ts": None, "date": f["flight_date"],
                    "type": "flight", "id": f["id"],
                    "label": "飞行 %s %s" % (f["flight_date"], f["aircraft"] or "")})
    for a in conn.execute("""SELECT id, kind, capture_time, upload_time, filename,
                            flight_id, status FROM media_assets
                            WHERE capture_time IS NOT NULL""").fetchall():
        out.append({"track": "capture_time", "ts": a["capture_time"],
                    "type": "media_capture", "id": a["id"],
                    "label": "拍摄 %s %s" % (a["kind"], a["filename"] or a["id"]),
                    "flight_id": a["flight_id"], "status": a["status"]})
    for a in conn.execute("""SELECT id, kind, upload_time, filename, status
                            FROM media_assets WHERE upload_time IS NOT NULL""").fetchall():
        out.append({"track": "upload_time", "ts": a["upload_time"],
                    "type": "media_upload", "id": a["id"],
                    "label": "上传完成 %s" % (a["filename"] or a["id"]),
                    "status": a["status"]})
    for t in conn.execute("""SELECT tr.id, tr.ordered_at, tr.ready_at, tr.status,
                             a.filename FROM transcriptions tr
                             JOIN media_assets a ON a.id=tr.asset_id""").fetchall():
        out.append({"track": "processing", "ts": t["ready_at"] or t["ordered_at"],
                    "type": "transcription", "id": t["id"],
                    "label": "语音转写%s (%s)" % (
                        "到达" if t["ready_at"] else "等待中", t["filename"] or ""),
                    "status": t["status"]})
    return out
