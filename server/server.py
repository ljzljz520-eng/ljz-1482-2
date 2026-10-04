# -*- coding: utf-8 -*-
import json, mimetypes, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import db, geo
import app as A

MEDIA_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".html": "text/html; charset=utf-8", ".json": "application/json",
               ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
               ".ico": "image/x-icon"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj, ctype="application/json; charset=utf-8", raw=None):
        body = raw if raw is not None else A.jdump(obj)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-User-Id")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,PUT,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def do_OPTIONS(self):
        self._send(204, {}, raw=b"")

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path); path = u.path
        q = {}
        if u.query:
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
        con = db.connect()
        try:
            r = self._route_get(con, path, q)
            if r is None:
                if path.startswith("/api/"):
                    return self._err(404, "no such endpoint: " + path)
                return self._static(path)
            if r == "":
                return
            self._send(200, r)
        except Exception as e:
            self._err(400, str(e))
        finally:
            con.close()

    def _route_get(self, con, path, q):
        user = A.get_user(self, con)
        if path == "/api/state":
            return self._state(con)
        if path == "/api/users":
            return db.rows_to_dicts(con.execute("SELECT * FROM users").fetchall())
        if path == "/api/plots":
            plots = db.rows_to_dicts(con.execute("SELECT * FROM plots ORDER BY id").fetchall())
            for p in plots:
                vs = con.execute("SELECT * FROM plot_versions WHERE plot_id=? ORDER BY version", (p["id"],)).fetchall()
                p["versions"] = [dict(v) for v in vs]
            return plots
        if path == "/api/flights":
            return db.rows_to_dicts(con.execute("SELECT * FROM flights ORDER BY flight_date,takeoff_at").fetchall())
        if path == "/api/media":
            rows = con.execute("SELECT * FROM media ORDER BY created_at DESC").fetchall()
            return [A.media_dict(con, r) for r in rows]
        if path == "/api/findings":
            return [A.finding_full(con, r["id"]) for r in con.execute(
                "SELECT id FROM findings WHERE merged_into_id IS NULL ORDER BY id").fetchall()]
        m = __import__("re").match(r"^/api/findings/(\d+)$", path)
        if m:
            return A.finding_full(con, int(m.group(1)))
        if path == "/api/conflicts":
            out = []
            for c in con.execute("SELECT * FROM conflicts ORDER BY status,id").fetchall():
                d = dict(c)
                d["finding_a_obj"] = A.finding_full(con, c["finding_a"])
                d["finding_b_obj"] = A.finding_full(con, c["finding_b"])
                out.append(d)
            return out
        if path == "/api/pending":
            return db.rows_to_dicts(con.execute("SELECT * FROM pending_attachments ORDER BY id DESC").fetchall())
        if path == "/api/reports":
            out = []
            for r in con.execute("SELECT * FROM reports ORDER BY id").fetchall():
                d = dict(r); d["package"] = json.loads(r["package_json"]) if r["package_json"] else None
                d.pop("package_json", None); d.pop("config_json", None)
                out.append(d)
            return out
        m = __import__("re").match(r"^/api/reports/(\d+)$", path)
        if m:
            r = con.execute("SELECT * FROM reports WHERE id=?", (m.group(1),)).fetchone()
            d = dict(r); d["package"] = json.loads(r["package_json"])
            return d
        if path == "/api/reviews":
            return db.rows_to_dicts(con.execute("""SELECT t.*, f.code finding_code, u.name assignee
                FROM review_tasks t LEFT JOIN findings f ON f.id=t.finding_id
                LEFT JOIN users u ON u.id=t.assigned_to ORDER BY t.id DESC""").fetchall())
        if path == "/api/timeline":
            return self._timeline(con, q)
        mm = __import__("re").match(r"^/api/media/(\d+)/raw$", path)
        if mm:
            return self._media_raw(con, int(mm.group(1)))
        mm = __import__("re").match(r"^/api/uploads/(\d+)$", path)
        if mm:
            sid = int(mm.group(1))
            s = con.execute("SELECT * FROM upload_sessions WHERE id=?", (sid,)).fetchone()
            if not s: raise ValueError("session not found")
            return {"session": dict(s), "received": A.received_indexes(con, sid)}
        mm = __import__("re").match(r"^/api/clips/(\d+)$", path)
        if mm:
            return dict(con.execute("SELECT * FROM clips WHERE id=?", (mm.group(1),)).fetchone())
        if path == "/api/health":
            return {"ok": True, "time": A.now()}
        return None

    def _state(self, con):
        def lq(sql, *a):
            return db.rows_to_dicts(con.execute(sql, a).fetchall())
        plots = lq("SELECT * FROM plots ORDER BY id")
        for p in plots:
            p["versions"] = lq("SELECT * FROM plot_versions WHERE plot_id=? ORDER BY version", p["id"])
        media = [A.media_dict(con, r) for r in con.execute("SELECT * FROM media ORDER BY id").fetchall()]
        return {
            "plots": plots,
            "flights": lq("SELECT * FROM flights ORDER BY flight_date,takeoff_at"),
            "media": media,
            "evidence": lq("SELECT * FROM evidence ORDER BY id"),
            "clips": lq("SELECT * FROM clips ORDER BY id"),
            "findings": [A.finding_full(con, r["id"]) for r in
                         con.execute("SELECT id FROM findings WHERE merged_into_id IS NULL ORDER BY id").fetchall()],
            "conflicts": lq("SELECT * FROM conflicts ORDER BY id"),
            "pending": lq("SELECT * FROM pending_attachments ORDER BY id"),
            "reports": lq("SELECT id,code,title,version,status,generated_at,dispatched_at FROM reports ORDER BY id"),
            "reviews": lq("SELECT * FROM review_tasks ORDER BY id DESC"),
            "exports": lq("SELECT * FROM exports ORDER BY id DESC"),
        }

    def _timeline(self, con, q):
        """统一时间线: 飞行 / 拍摄 / 上传 / 发现 / 转写 分泳道, 三个时间严格分开。"""
        events = []
        for f in con.execute("SELECT * FROM flights").fetchall():
            events.append({"id": "f%d" % f["id"], "lane": "flight", "date": f["flight_date"],
                           "at": f["takeoff_at"], "title": "飞行 %s" % f["code"], "ref_id": f["id"],
                           "flight_id": f["id"]})
        for m in con.execute("SELECT * FROM media").fetchall():
            if m["captured_at"]:
                events.append({"id": "cap%d" % m["id"], "lane": "capture", "at": m["captured_at"],
                               "title": "%s 拍摄 %s" % ({"image": "照片","video":"视频","audio":"语音"}.get(m["kind"], m["kind"]), m["filename"]),
                               "ref_id": m["id"], "flight_id": m["flight_id"]})
            if m["uploaded_at"]:
                events.append({"id": "up%d" % m["id"], "lane": "upload", "at": m["uploaded_at"],
                               "title": "上传完成 %s" % m["filename"], "ref_id": m["id"],
                               "status": m["status"]})
            if m["transcript_status"] == "done" and m["uploaded_at"]:
                events.append({"id": "tr%d" % m["id"], "lane": "transcript", "at": m["uploaded_at"],
                               "actual_at": A.now(), "title": "语音转写晚到 %s" % m["filename"],
                               "ref_id": m["id"], "late": True})
        for fnd in con.execute("SELECT * FROM findings").fetchall():
            if fnd["first_seen_at"]:
                events.append({"id": "fn%d" % fnd["id"], "lane": "finding", "at": fnd["first_seen_at"],
                               "title": "发现 %s %s" % (fnd["code"], fnd["title"]),
                               "ref_id": fnd["id"], "flight_id": fnd["first_seen_flight_id"],
                               "status": fnd["status"]})
        # 同一素材: 拍摄时间 vs 上传时间差(校验三时间分离)
        events.sort(key=lambda e: e.get("at") or "")
        lane = q.get("lane")
        if lane:
            events = [e for e in events if e["lane"] == lane]
        return {"events": events,
                "note": "flight_date 飞行日期 / captured_at 拍摄时间 / uploaded_at 上传时间 三者独立存储与展示"}

    def _media_raw(self, con, mid):
        m = con.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
        if not m:
            self._err(404, "not found"); return ""
        if m["status"] != "uploaded" or not m["object_key"]:
            self._err(409, "media not uploaded: " + m["status"]); return ""
        p = db.object_path(m["object_key"])
        if not os.path.exists(p):
            self._err(410, "object missing in storage (导出缺失/对象丢失)"); return ""
        ctype = m["mime"] or mimetypes.guess_type(m["filename"] or "")[0] or "application/octet-stream"
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, None, ctype=ctype, raw=data)
        return ""

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        fp = os.path.normpath(os.path.join(A.STATIC, path.lstrip("/")))
        if not fp.startswith(A.STATIC) or not os.path.isfile(fp):
            # SPA fallback
            fp = os.path.join(A.STATIC, "index.html")
            if not os.path.isfile(fp):
                return self._err(404, "not found")
        ext = os.path.splitext(fp)[1]
        with open(fp, "rb") as f:
            data = f.read()
        self._send(200, None, ctype=MEDIA_TYPES.get(ext, "application/octet-stream"), raw=data)

    # ---- POST / PATCH ----
    def do_POST(self):
        self._mutate("POST")

    def do_PATCH(self):
        self._mutate("PATCH")

    def _mutate(self, method):
        u = urlparse(self.path); path = u.path
        con = db.connect()
        user = A.get_user(self, con)
        try:
            # 二进制分块不走 JSON
            m = __import__("re").match(r"^/api/uploads/(\d+)/chunks/(\d+)$", path)
            if m and method == "PUT" or (m and method == "POST"):
                return self._handle_chunk(con, int(m.group(1)), int(m.group(2)))
            payload = A.body_json(self)
            r = self._route_mut(con, path, payload, user, method)
            if r == True:
                self._send(200, {"ok": True})
            elif isinstance(r, tuple):
                self._send(r[0], r[1])
            else:
                self._send(200, r)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._err(400, str(e))
        finally:
            con.close()

    def _handle_chunk(self, con, sid, idx):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n)
        sha = self.headers.get("X-Chunk-Sha256")
        r = A.put_chunk(con, sid, idx, data, sha)
        mrec = con.execute("SELECT media_id FROM upload_sessions WHERE id=?", (sid,)).fetchone()
        if r.get("completed") and mrec:
            mm = con.execute("SELECT kind FROM media WHERE id=?", (mrec["media_id"],)).fetchone()
            if mm["kind"] == "audio":
                A.taskq.put(mrec["media_id"])
        self._send(200, r)

    def _route_mut(self, con, path, p, user, method):
        import re
        # plots / boundary revisions
        if path == "/api/plots" and method == "POST":
            cur = con.execute("INSERT INTO plots(code,name,created_at) VALUES(?,?,?)",
                              (p["code"], p["name"], A.now()))
            pid = cur.lastrowid
            con.execute("""INSERT INTO plot_versions(plot_id,version,boundary_geojson,area_mu,source,
                rms_error_m,created_by,created_at,revised_reason) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (pid, 1, json.dumps(p["boundary_geojson"]), p.get("area_mu"),
                         p.get("source", "手绘"), p.get("rms_error_m", 5.0), user["id"], A.now(), "建块初版"))
            con.commit()
            return {"plot_id": pid}
        m = re.match(r"^/api/plots/(\d+)/revisions$", path)
        if m and method == "POST":
            pid = int(m.group(1))
            ver = con.execute("SELECT COALESCE(MAX(version),0)+1 v FROM plot_versions WHERE plot_id=?", (pid,)).fetchone()["v"]
            con.execute("""INSERT INTO plot_versions(plot_id,version,boundary_geojson,area_mu,source,
                rms_error_m,revised_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (pid, ver, json.dumps(p["boundary_geojson"]), p.get("area_mu"), p.get("source"),
                 p.get("rms_error_m"), p.get("reason", "边界修订"), user["id"], A.now()))
            con.execute("UPDATE plots SET active_version=? WHERE id=?", (ver, pid))
            changed = A.rejudge_plots(con)   # 修订后重新判断全部发现归属
            return {"version": ver, "rejudged": changed}
        # flights
        if path == "/api/flights" and method == "POST":
            if not p.get("flight_date"):
                raise ValueError("flight_date 必填 (飞行日期, 与拍摄时间分开)")
            cur = con.execute("""INSERT INTO flights(code,drone_model,camera_model,pilot,flight_date,
                takeoff_at,footprint_geojson,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (p["code"], p.get("drone_model"), p.get("camera_model"), p.get("pilot"),
                 p["flight_date"], p.get("takeoff_at"),
                 json.dumps(p["footprint_geojson"]) if p.get("footprint_geojson") else None,
                 p.get("note"), A.now()))
            con.commit()
            return {"flight_id": cur.lastrowid}
        # media metadata (no bytes yet)
        if path == "/api/media" and method == "POST":
            cur = con.execute("""INSERT INTO media(client_uid,kind,filename,mime,status,captured_at,
                flight_id,uploader_id,created_at,duration_ms,transcript_status)
                VALUES(?,?,?,?,'uploading',?,?,?,?,'queued')""",
                (p["client_uid"], p["kind"], p.get("filename"), p.get("mime"),
                 p.get("captured_at"), p.get("flight_id"), user["id"], A.now()))
            con.commit()
            return {"media_id": cur.lastrowid}
        # upload sessions (supports placeholders pending_id)
        if path == "/api/uploads" and method == "POST":
            return A.create_upload_session(con, p, user)
        # georeference + evidence
        if path == "/api/evidence" and method == "POST":
            g = geo.georeference(p)
            cur = con.execute("""INSERT INTO evidence(media_id,clip_id,frame_time_ms,frame_cx,frame_cy,
                fov_h,fov_v,drone_lng,drone_lat,drone_alt,ground_elevation,yaw,pitch,roll,camera_model,
                dem_source,drone_hrms,footprint_geojson,center_lng,center_lat,radius_m,
                horizontal_error_m,geo_method,computed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["media_id"], p.get("clip_id"), p.get("frame_time_ms"), p["frame_cx"], p["frame_cy"],
                 p.get("fov_h"), p.get("fov_v"), p["drone_lng"], p["drone_lat"], p["drone_alt"],
                 p.get("ground_elevation", 0), p.get("yaw", 0), p.get("pitch", -90), p.get("roll", 0),
                 p.get("camera_model"), p.get("dem_source"), p.get("drone_hrms", 5),
                 json.dumps(g["footprint_geojson"]), g["center_lng"], g["center_lat"], g["radius_m"],
                 g["horizontal_error_m"], g["geo_method"], A.now()))
            eid = cur.lastrowid
            con.commit()
            mm = con.execute("SELECT kind FROM media WHERE id=?", (p["media_id"],)).fetchone()
            return {"evidence_id": eid, **g}
        # clips (video cut with source timecodes)
        m = re.match(r"^/api/media/(\d+)/clips$", path)
        if m and method == "POST":
            mid = int(m.group(1))
            prev = con.execute("SELECT COALESCE(MAX(cut_revision),0)+1 v FROM clips WHERE media_id=?", (mid,)).fetchone()["v"]
            def tc(ms):
                return "%02d:%02d:%02d.%03d" % (ms//3600000, ms%3600000//60000, ms%60000//1000, ms%1000)
            cur = con.execute("""INSERT INTO clips(media_id,src_start_ms,src_end_ms,start_tc,end_tc,
                cut_revision,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (mid, p["src_start_ms"], p["src_end_ms"], tc(p["src_start_ms"]), tc(p["src_end_ms"]),
                 prev, p.get("note", "裁切片段(保留源时间码)"), user["id"], A.now()))
            con.commit()
            return {"clip_id": cur.lastrowid, "cut_revision": prev,
                    "start_tc": tc(p["src_start_ms"]), "end_tc": tc(p["src_end_ms"])}
        # findings
        if path == "/api/findings" and method == "POST":
            missing = [k for k in ("location_lng","location_lat","locate_basis","locate_error_m") if k not in p]
            if missing:
                raise ValueError("缺少定位字段: %s (必须明确写定位依据与误差)" % ",".join(missing))
            return A.create_finding(con, p, user)
        m = re.match(r"^/api/findings/(\d+)/inspections$", path)
        if m and method == "POST":
            iid = A.add_inspection(con, int(m.group(1)), p, user)
            return A.finding_full(con, int(m.group(1)))
        m = re.match(r"^/api/findings/(\d+)/review$", path)
        if m and method == "POST":
            fid = int(m.group(1))
            con.execute("UPDATE findings SET status='reviewed',reviewed_by=?,reviewed_at=? WHERE id=?",
                        (user["id"], A.now(), fid))
            con.commit()
            return A.finding_full(con, fid)
        m = re.match(r"^/api/findings/(\d+)/suggestions$", path)
        if m and method == "POST":
            fid = int(m.group(1))
            if not p.get("content", "").strip():
                raise ValueError("建议内容必填, 且只能来自用户填写")
            con.execute("INSERT INTO suggestions(finding_id,author_id,content,created_at) VALUES(?,?,?,?)",
                        (fid, user["id"], p["content"].strip(), A.now()))
            con.commit()
            return A.finding_full(con, fid)
        m = re.match(r"^/api/findings/(\d+)/status$", path)
        if m and method == "PATCH":
            fid = int(m.group(1))
            con.execute("UPDATE findings SET status=? WHERE id=?", (p["status"], fid))
            con.commit()
            return A.finding_full(con, fid)
        # conflicts
        m = re.match(r"^/api/conflicts/(\d+)/resolve$", path)
        if m and method == "POST":
            return A.resolve_conflict(con, int(m.group(1)), p, user)
        # pending attachments (placeholders / cross-device)
        if path == "/api/pending" and method == "POST":
            cur = con.execute("""INSERT INTO pending_attachments(client_uid,owner_device,kind,filename,note,
                finding_client_ref,finding_id,status,created_at,synced_at) VALUES(?,?,?,?,?,?,?, 'placeholder',?,?)""",
                (p["client_uid"], p.get("owner_device"), p.get("kind"), p.get("filename"),
                 p.get("note"), p.get("finding_client_ref"), p.get("finding_id"), A.now(), A.now()))
            con.commit()
            return dict(con.execute("SELECT * FROM pending_attachments WHERE id=?", (cur.lastrowid,)).fetchone())
        m = re.match(r"^/api/pending/(\d+)$", path)
        if m and method == "PATCH":
            pid = int(m.group(1))
            pa = con.execute("SELECT * FROM pending_attachments WHERE id=?", (pid,)).fetchone()
            if p.get("finding_id"):
                con.execute("UPDATE pending_attachments SET finding_id=?,synced_at=? WHERE id=?",
                            (p["finding_id"], A.now(), pid))
            if p.get("status") == "superseded":
                con.execute("UPDATE pending_attachments SET status='superseded' WHERE id=?", (pid,))
            con.commit()
            return dict(con.execute("SELECT * FROM pending_attachments WHERE id=?", (pid,)).fetchone())
        # reports
        if path == "/api/reports/generate" and method == "POST":
            return A.generate_report(con, p, user)
        m = re.match(r"^/api/reports/(\d+)/dispatch$", path)
        if m and method == "POST":
            return A.dispatch_review(con, int(m.group(1)), p, user)
        m = re.match(r"^/api/reports/(\d+)/export$", path)
        if m and method == "POST":
            return A.export_zip(con, int(m.group(1)), user)
        # review tasks completion (original report reference preserved)
        m = re.match(r"^/api/reviews/(\d+)$", path)
        if m and method == "PATCH":
            tid = int(m.group(1))
            con.execute("UPDATE review_tasks SET status=?,note=COALESCE(?,note),completed_at=? WHERE id=?",
                        (p.get("status", "done"), p.get("note"), A.now(), tid))
            con.commit()
            return {"ok": True}
        # media transcript late PATCH (simulate provider callback)
        m = re.match(r"^/api/media/(\d+)/transcript$", path)
        if m and method == "PATCH":
            mid = int(m.group(1))
            con.execute("UPDATE media SET transcript=?,transcript_status='done' WHERE id=?",
                        (p.get("transcript", ""), mid))
            con.commit()
            return {"ok": True}
        raise ValueError("no route for %s %s" % (method, path))

    do_PUT = do_POST

def run(port=8000):
    db.init()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Aerial farm workbench on http://localhost:%d" % port)
    srv.serve_forever()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    run(a.port)
