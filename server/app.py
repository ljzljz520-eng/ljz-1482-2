# -*- coding: utf-8 -*-
"""航拍农田报告工作台 — stdlib-only backend.
Run:  python3 server/app.py  [--port 8000]
"""
import argparse, hashlib, json, os, queue, random, re, shutil, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db
import geo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, "static")
SIMILARITY_DETECT = True   # 图像相似仅用于“提示”冲突，绝不自动合并
DB = db
taskq = queue.Queue()

# ---------------- helpers ----------------
def now():
    return db.now()

def jdump(o):
    return json.dumps(o, ensure_ascii=False).encode("utf-8")

def parse_path(path):
    p = path.split("?", 1)
    return p[0], parse_qs(p[1]) if len(p) > 1 else {}

def body_json(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(n) if n else b""
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))

def get_user(handler, con):
    uid = handler.headers.get("X-User-Id") or "1"
    r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else {"id": 1, "name": "演示用户", "role": "agronomist"}

def next_code(con, prefix, table):
    n = con.execute("SELECT COUNT(*) c FROM %s" % table).fetchone()["c"] + 1
    while True:
        code = "%s-%04d" % (prefix, n)
        if not con.execute("SELECT 1 FROM %s WHERE code=?" % table, (code,)).fetchone():
            return code
        n += 1

def assign_plot(con, lng, lat):
    """返回 (plot_id, version_id, inside_list) 基于最新地块版本。"""
    hits = []
    rows = con.execute("""SELECT pv.id vid, pv.plot_id pid, pv.boundary_geojson gj,
                          pv.version ver FROM plot_versions pv
                          JOIN plots p ON p.active_version=pv.version AND p.id=pv.plot_id""").fetchall()
    for r in rows:
        if geo.point_in_polygon(lng, lat, r["gj"]):
            hits.append((r["pid"], r["vid"]))
    return (hits[0] if hits else (None, None)), hits

def media_dict(con, r):
    d = dict(r)
    if r["kind"] == "audio":
        d["has_audio"] = True
    return d

def evidence_dict(con, ev):
    d = dict(ev)
    m = con.execute("SELECT * FROM media WHERE id=?", (ev["media_id"],)).fetchone()
    d["media"] = dict(m) if m else None
    if ev["clip_id"]:
        c = con.execute("SELECT * FROM clips WHERE id=?", (ev["clip_id"],)).fetchone()
        d["clip"] = dict(c) if c else None
    return d

def finding_full(con, fid):
    f = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if not f:
        return None
    d = dict(f)
    d["plot"] = dict(con.execute("SELECT p.*, pv.version, pv.boundary_geojson FROM plots p "
                                 "JOIN plot_versions pv ON pv.id=?", (f["plot_version_id"],)).fetchone()) \
                if f["plot_version_id"] else None
    insps = con.execute("SELECT * FROM inspections WHERE finding_id=? ORDER BY inspected_at", (fid,)).fetchall()
    d["inspections"] = []
    for ins in insps:
        i = dict(ins)
        flight = con.execute("SELECT * FROM flights WHERE id=?", (ins["flight_id"],)).fetchone()
        i["flight"] = dict(flight) if flight else None
        evs = con.execute("""SELECT fe.*, u.name annotator FROM finding_evidence fe
                             LEFT JOIN users u ON u.id=fe.annotated_by
                             WHERE fe.inspection_id=?""", (ins["id"],)).fetchall()
        i["evidence"] = []
        for fe in evs:
            ev = con.execute("SELECT * FROM evidence WHERE id=?", (fe["evidence_id"],)).fetchone()
            ed = evidence_dict(con, ev)
            ed["annotator"] = fe["annotator"]
            ed["note"] = fe["note"]
            i["evidence"].append(ed)
        d["inspections"].append(i)
    d["suggestions"] = [dict(r) for r in con.execute(
        "SELECT s.*, u.name author FROM suggestions s LEFT JOIN users u ON u.id=s.author_id WHERE finding_id=?",
        (fid,)).fetchall()]
    hist = con.execute("""SELECT h.*, p.code plot_code FROM finding_plot_history h
                          LEFT JOIN plots p ON p.id=h.plot_id WHERE finding_id=? ORDER BY h.id""",
                       (fid,)).fetchall()
    d["plot_history"] = [dict(r) for r in hist]
    return d

# ---------------- media / chunks ----------------
def create_upload_session(con, payload, user):
    uid = payload["client_uid"]
    total = int(payload["total_size"]); csz = int(payload["chunk_size"])
    sha = payload["sha256"].lower()
    ext = (payload.get("filename") or "bin").rsplit(".", 1)[-1][:6]
    # 整对象秒传: 内容寻址已存在 -> 去重, 不重复占存储
    key = db.object_key_for(sha, ext)
    existing = con.execute("SELECT * FROM media WHERE sha256=? AND status='uploaded'", (sha,)).fetchone()
    mid = None
    if payload.get("pending_id"):
        pa = con.execute("SELECT * FROM pending_attachments WHERE id=?", (payload["pending_id"],)).fetchone()
        if pa and pa["media_id"]:
            mid = pa["media_id"]
    if mid is None:
        cur = con.execute("""INSERT INTO media(client_uid,kind,filename,mime,size,sha256,object_key,
            status,duration_ms,width,height,captured_at,uploader_id,created_at,transcript_status)
            VALUES(?,?,?,?,?,?,?,'uploading',?,?,?,?,?,?,'queued')""",
            (uid, payload["kind"], payload.get("filename"), payload.get("mime"), total, sha, key,
             payload.get("duration_ms"), payload.get("width"), payload.get("height"),
             payload.get("captured_at"), user["id"], now()))
        mid = cur.lastrowid
    cur = con.execute("""INSERT INTO upload_sessions(media_id,client_uid,chunk_size,total_size,sha256,
        pending_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?, 'open',?,?)""",
        (mid, uid + "-" + sha[:8], csz, total, sha, payload.get("pending_id"), now(), now()))
    sid = cur.lastrowid
    instant = bool(existing) and os.path.exists(db.object_path(key))
    if instant:
        # 秒传: 标记全部分块已存在并直接完成
        for i in range((total + csz - 1) // csz):
            con.execute("INSERT OR REPLACE INTO chunks(session_id,idx,size,sha256,received_at) VALUES(?,?,?,?,?)",
                        (sid, i, min(csz, total - i*csz), "", now()))
        complete_session(con, sid, reuse_key=key)
    con.commit()
    return {"session_id": sid, "media_id": mid, "instant": instant,
            "received": received_indexes(con, sid)}

def received_indexes(con, sid):
    return [r["idx"] for r in con.execute("SELECT idx FROM chunks WHERE session_id=? ORDER BY idx", (sid,))]

def put_chunk(con, sid, idx, data, sha):
    s = con.execute("SELECT * FROM upload_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise ValueError("session not found")
    # 重复块: 已存在则跳过 (幂等), 支持断网/崩溃后续传
    if con.execute("SELECT 1 FROM chunks WHERE session_id=? AND idx=?", (sid, idx)).fetchone():
        con.commit()
        return {"idx": idx, "dedup": True, "received": received_indexes(con, sid)}
    if sha and hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("chunk checksum mismatch idx=%d" % idx)
    with open(db.chunk_path(sid, idx), "wb") as f:
        f.write(data)
    con.execute("INSERT INTO chunks(session_id,idx,size,sha256,received_at) VALUES(?,?,?,?,?)",
                (sid, idx, len(data), sha or "", now()))
    con.execute("UPDATE upload_sessions SET updated_at=? WHERE id=?", (now(), sid))
    done = len(received_indexes(con, sid)) * s["chunk_size"] >= s["total_size"]
    if done:
        complete_session(con, sid)
    con.commit()
    return {"idx": idx, "dedup": False, "received": received_indexes(con, sid), "completed": done}

def complete_session(con, sid, reuse_key=None):
    s = con.execute("SELECT * FROM upload_sessions WHERE id=?", (sid,)).fetchone()
    n = (s["total_size"] + s["chunk_size"] - 1) // s["chunk_size"]
    got = set(received_indexes(con, sid))
    if len(got) != n:
        return False
    if reuse_key and os.path.exists(db.object_path(reuse_key)):
        # 秒传: 对象存储中已有相同 sha256, 直接复用, 不重复落盘/拼装
        con.execute("UPDATE media SET status='uploaded', object_key=?, uploaded_at=? WHERE id=?",
                    (reuse_key, now(), s["media_id"]))
        con.execute("UPDATE upload_sessions SET status='completed',updated_at=? WHERE id=?", (now(), sid))
        if s["pending_id"]:
            con.execute("UPDATE pending_attachments SET status='uploaded',media_id=?,synced_at=?,uploaded_at=? WHERE id=?",
                        (s["media_id"], now(), now(), s["pending_id"]))
        return True
    target = db.object_path(s["sha256"] + ".bin")  # ext fixed below
    m = con.execute("SELECT * FROM media WHERE id=?", (s["media_id"],)).fetchone()
    ext = (m["filename"] or "bin").rsplit(".", 1)[-1][:6] if m["filename"] else "bin"
    target = db.object_path(db.object_key_for(s["sha256"], ext))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    h = hashlib.sha256()
    with open(target, "wb") as out:
        for i in range(n):
            with open(db.chunk_path(sid, i), "rb") as f:
                buf = f.read()
                h.update(buf); out.write(buf)
    if h.hexdigest() != s["sha256"]:
        os.remove(target)
        raise ValueError("assembled object checksum mismatch")
    con.execute("UPDATE media SET status='uploaded', object_key=?, uploaded_at=? WHERE id=?",
                (db.object_key_for(s["sha256"], ext), now(), s["media_id"]))
    con.execute("UPDATE upload_sessions SET status='completed',updated_at=? WHERE id=?", (now(), sid))
    if s["pending_id"]:
        con.execute("UPDATE pending_attachments SET status='uploaded',media_id=?,synced_at=?,uploaded_at=? WHERE id=?",
                    (s["media_id"], now(), now(), s["pending_id"]))
    return True

# ---------------- findings / inspections / conflicts ----------------
def create_finding(con, payload, user):
    lng, lat = float(payload["location_lng"]), float(payload["location_lat"])
    (pid, pvid), _ = assign_plot(con, lng, lat)
    cur = con.execute("""INSERT INTO findings(code,title,ftype,severity,status,plot_id,plot_version_id,
        location_lng,location_lat,locate_basis,locate_error_m,first_seen_flight_id,first_seen_at,
        created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (next_code(con, "FND", "findings"), payload["title"], payload.get("ftype"),
         payload.get("severity"), payload.get("status", "draft"), pid, pvid, lng, lat,
         payload["locate_basis"], float(payload["locate_error_m"]),
         payload.get("flight_id"), payload.get("first_seen_at"), user["id"], now()))
    fid = cur.lastrowid
    con.execute("""INSERT INTO finding_plot_history(finding_id,plot_id,plot_version_id,inside,reason,changed_at)
        VALUES(?,?,?,?,?,?)""", (fid, pid, pvid, 1 if pid else 0, "初始归属(创建时)", now()))
    con.commit()
    if payload.get("flight_id"):
        add_inspection(con, fid, {"flight_id": payload["flight_id"], "inspected_at":
                       payload.get("first_seen_at") or now(), "evidence_ids": payload.get("evidence_ids", [])}, user)
    detect_conflicts(con, fid)
    con.commit()
    return finding_full(con, fid)

def add_inspection(con, fid, payload, user):
    cur = con.execute("""INSERT INTO inspections(finding_id,flight_id,inspected_at,note,created_at)
        VALUES(?,?,?,?,?)""", (fid, payload["flight_id"], payload["inspected_at"],
                               payload.get("note"), now()))
    iid = cur.lastrowid
    for eid in payload.get("evidence_ids", []):
        con.execute("""INSERT OR IGNORE INTO finding_evidence(inspection_id,evidence_id,annotated_by,note,created_at)
            VALUES(?,?,?,?,?)""", (iid, eid, user["id"], payload.get("evidence_note"), now()))
    # 同一巡检轮次、同一位置的两人标注 -> 冲突(需人工处理, 不自动合并)
    detect_conflicts(con, fid, same_inspection=True)
    con.commit()
    return iid

def detect_conflicts(con, fid, same_inspection=False):
    f = con.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    others = con.execute("""SELECT * FROM findings WHERE id<>? AND status NOT IN ('merged')
                            AND merged_into_id IS NULL""", (fid,)).fetchall()
    for o in others:
        if o["location_lng"] is None:
            continue
        dist = geo.haversine(f["location_lat"], f["location_lng"], o["location_lat"], o["location_lng"])
        # 位置临近且误差圆相交
        if dist <= max(f["locate_error_m"] + o["locate_error_m"], 30.0) and dist < 60.0:
            already = con.execute("""SELECT 1 FROM conflicts WHERE status='open' AND
                ((finding_a=? AND finding_b=?) OR (finding_a=? AND finding_b=?))""",
                (fid, o["id"], o["id"], fid)).fetchone()
            if already:
                continue
            sim = round(max(0.0, 1.0 - dist / 60.0), 3) if SIMILARITY_DETECT else None
            same_round = _same_inspection_round(con, fid, o["id"])
            reason = "两人标注同一位置(同一巡检轮次)" if same_round else "位置临近/疑似重复(图像相似仅作提示)"
            con.execute("""INSERT INTO conflicts(finding_a,finding_b,reason,distance_m,image_similarity,status,created_at)
                VALUES(?,?,?,?,?,'open',?)""", (fid, o["id"], reason, round(dist, 1), sim, now()))

def _same_inspection_round(con, a, b):
    ra = con.execute("SELECT flight_id FROM inspections WHERE finding_id=? ORDER BY id DESC LIMIT 1", (a,)).fetchone()
    rb = con.execute("SELECT flight_id FROM inspections WHERE finding_id=? ORDER BY id DESC LIMIT 1", (b,)).fetchone()
    return bool(ra and rb and ra["flight_id"] == rb["flight_id"])

def resolve_conflict(con, cid, payload, user):
    c = con.execute("SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone()
    action = payload["action"]                      # merge | keep_separate
    note = payload.get("resolution_note", "")
    if action == "merge":
        survivor = int(payload["survivor_id"])
        loser = c["finding_b"] if survivor == c["finding_a"] else c["finding_a"]
        if survivor not in (c["finding_a"], c["finding_b"]):
            raise ValueError("survivor must be one of the conflict pair")
        # 多次巡检证据迁移: 把被合并方的全部巡检轮次整体搬到保留方(证据不丢)
        for ins in con.execute("SELECT id FROM inspections WHERE finding_id=?", (loser,)).fetchall():
            exists = con.execute("""SELECT i.id FROM inspections i WHERE i.finding_id=?
                AND i.flight_id=(SELECT flight_id FROM inspections WHERE id=?)""",
                                 (survivor, ins["id"])).fetchone()
            if exists:
                con.execute("UPDATE finding_evidence SET inspection_id=? WHERE inspection_id=?",
                            (exists["id"], ins["id"]))
                con.execute("DELETE FROM inspections WHERE id=?", (ins["id"],))
            else:
                con.execute("UPDATE inspections SET finding_id=? WHERE id=?", (survivor, ins["id"]))
        con.execute("UPDATE findings SET status='merged',merged_into_id=?,merge_note=? WHERE id=?",
                    (survivor, note or "冲突合并", loser))
        con.execute("""UPDATE conflicts SET status='merged',survivor_id=?,resolution_note=?,resolved_by=?,resolved_at=?
            WHERE id=?""", (survivor, note, user["id"], now(), cid))
    else:
        con.execute("""UPDATE conflicts SET status='kept_separate',resolution_note=?,resolved_by=?,resolved_at=?
            WHERE id=?""", (note or "确认为不同事件", user["id"], now(), cid))
    con.commit()
    return dict(con.execute("SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone())

def rejudge_plots(con):
    """地块边界修订后, 用每个地块【最新版本】重新判断全部发现归属。
    已派出复核保留其派出时刻的报告引用(在 review_tasks 中快照, 不随之改写)。"""
    fs = con.execute("SELECT * FROM findings WHERE merged_into_id IS NULL").fetchall()
    changed = []
    for f in fs:
        (pid, pvid), hits = assign_plot(con, f["location_lng"], f["location_lat"])
        if pvid != f["plot_version_id"] and pid == f["plot_id"]:
            # 仍在同一地块: 仅刷新到最新边界版本, 不算归属变化
            con.execute("UPDATE findings SET plot_version_id=? WHERE id=?", (pvid, f["id"]))
        if pid != f["plot_id"]:
            # 仅跨地块(含 有<->无)才是真正的归属重判
            con.execute("UPDATE findings SET plot_id=?,plot_version_id=? WHERE id=?", (pid, pvid, f["id"]))
            con.execute("""INSERT INTO finding_plot_history(finding_id,plot_id,plot_version_id,inside,reason,changed_at)
                VALUES(?,?,?,?,?,?)""", (f["id"], pid, pvid, 1 if pid else 0,
                    "边界修订后跨地块重判(原 plot_id=%s)" % f["plot_id"], now()))
            changed.append({"finding_id": f["id"], "code": f["code"],
                            "old_plot_id": f["plot_id"], "new_plot_id": pid,
                            "old_plot_version_id": f["plot_version_id"], "new_plot_version_id": pvid})
    con.commit()
    return changed

# ---------------- reports / exports ----------------
def generate_report(con, payload, user):
    """预览包/正式包: 仅含 reviewed 发现 + 用户填写的建议; 不写入任何自动农事处置。"""
    cfg = payload.get("config_json") or {}
    rows = con.execute("""SELECT * FROM findings WHERE status='reviewed' AND merged_into_id IS NULL
                          ORDER BY COALESCE(first_seen_at,created_at)""").fetchall()
    items = []
    for f in rows:
        sugs = [s["content"] for s in con.execute(
            "SELECT content FROM suggestions WHERE finding_id=? ORDER BY id", (f["id"],)).fetchall()]
        items.append({"finding_id": f["id"], "code": f["code"], "title": f["title"],
                      "ftype": f["ftype"], "severity": f["severity"],
                      "plot_id": f["plot_id"], "plot_version_id": f["plot_version_id"],
                      "location": [f["location_lng"], f["location_lat"]],
                      "locate_basis": f["locate_basis"], "locate_error_m": f["locate_error_m"],
                      "suggestions_user": sugs,
                      "note": "仅汇总用户填写建议; 系统不自动决定农事处置"})
    pkg = {"generated_at": now(), "by_user": user["name"],
           "scope": "reviewed findings only", "items": items,
           "disclaimer": "本预览包只包含已审核发现与用户填写的建议, 不包含自动农事处置结论"}
    ver = (con.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"] + 1)
    cur = con.execute("""INSERT INTO reports(code,title,version,status,config_json,package_json,
        generated_by,generated_at) VALUES(?,?,?,?,?,?,?,?)""",
        ("RPT-%04d" % ver, payload.get("title", "航拍巡检报告 %d 期" % ver),
         cfg.get("version", ver), "preview", json.dumps(cfg, ensure_ascii=False),
         json.dumps(pkg, ensure_ascii=False), user["id"], now()))
    rid = cur.lastrowid
    for it in items:
        con.execute("""INSERT INTO report_findings(report_id,finding_id,snap_plot_id,snap_plot_version_id,
            snap_location_lng,snap_location_lat) VALUES(?,?,?,?,?,?)""",
            (rid, it["finding_id"], it["plot_id"], it["plot_version_id"],
             it["location"][0], it["location"][1]))
    con.commit()
    return {"report_id": rid, "package": pkg}

def dispatch_review(con, rid, payload, user):
    r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    tasks = []
    for fid in payload["finding_ids"]:
        cur = con.execute("""INSERT INTO review_tasks(code,finding_id,dispatched_from_report_id,
            report_code_snap,report_version_snap,assigned_to,status,note,due_at,created_at)
            VALUES(?,?,?,?,?,?, 'pending',?,?,?)""",
            (next_code(con, "REV", "review_tasks"), fid, rid, r["code"], r["version"],
             payload.get("assigned_to"), payload.get("note"), payload.get("due_at"), now()))
        tasks.append(cur.lastrowid)
    con.commit()
    con.execute("UPDATE reports SET status='dispatched',dispatched_at=? WHERE id=?", (now(), rid))
    con.commit()
    return {"tasks": tasks, "report_code": r["code"], "report_version": r["version"]}

def export_zip(con, rid, user):
    r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    pkg = json.loads(r["package_json"])
    cur = con.execute("INSERT INTO exports(report_id,status,created_by,created_at) VALUES(?, 'running',?,?)",
                      (rid, user["id"], now()))
    eid = cur.lastrowid; con.commit()
    zpath = os.path.join(db.EXPORT_DIR, "report_%d_export_%d.zip" % (rid, eid))
    missing = []; count = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.json", json.dumps(pkg, ensure_ascii=False, indent=2))
        for it in pkg["items"]:
            f = con.execute("SELECT * FROM findings WHERE id=?", (it["finding_id"],)).fetchone()
            evs = con.execute("""SELECT e.* FROM evidence e
                JOIN finding_evidence fe ON fe.evidence_id=e.id
                JOIN inspections ins ON ins.id=fe.inspection_id
                WHERE ins.finding_id=?""", (it["finding_id"],)).fetchall()
            for ev in evs:
                m = con.execute("SELECT * FROM media WHERE id=?", (ev["media_id"],)).fetchone()
                if not m or m["status"] != "uploaded" or not m["object_key"] or \
                   not os.path.exists(db.object_path(m["object_key"])):
                    missing.append({"finding_code": it["code"], "media_id": ev["media_id"],
                                    "reason": "对象缺失/未上传完成" if not m else "对象存储中文件丢失"})
                    continue
                z.write(db.object_path(m["object_key"]),
                        "media/f%s_%s" % (it["finding_id"], os.path.basename(m["object_key"])))
                count += 1
    status = "missing" if missing else "done"
    con.execute("UPDATE exports SET status=?,package_path=?,missing_json=?,file_count=?,completed_at=? WHERE id=?",
                (status, zpath, json.dumps(missing, ensure_ascii=False), count, now(), eid))
    con.commit()
    return {"export_id": eid, "status": status, "file_count": count, "missing": missing, "path": zpath}

# ---------------- background: late transcription ----------------
def transcription_worker():
    """模拟语音转写晚到: 上传完成后转写结果异步 PATCH 回来。"""
    while True:
        mid = taskq.get()
        try:
            time.sleep(random.uniform(2.0, 6.0))
            con = db.connect()
            m = con.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
            if m and m["kind"] == "audio":
                text = "【自动转写·晚到】%s 巡检语音：疑似异常，建议现场复核。" % (m["captured_at"] or now())
                con.execute("UPDATE media SET transcript=?,transcript_status='done' WHERE id=?", (text, mid))
                con.commit()
            con.close()
        except Exception as e:
            print("transcription error", e)
        finally:
            taskq.task_done()

for _ in range(2):
    threading.Thread(target=transcription_worker, daemon=True).start()
