# -*- coding: utf-8 -*-
"""Seed demonstration data through the real HTTP API.
Run while server is up: python3 server/seed.py
"""
import hashlib, io, json, os, random, time, urllib.request

BASE = os.environ.get("BASE", "http://localhost:8000")

def req(method, path, body=None, raw=None, headers=None):
    url = BASE + path
    data = None
    h = {"Content-Type": "application/json", "X-User-Id": "1"}
    if headers: h.update(headers)
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(r) as resp:
        ct = resp.headers.get("Content-Type", "")
        out = resp.read()
        return json.loads(out) if "json" in ct else out

def box(cx, cy, dx, dy):
    return {"type": "Polygon", "coordinates": [[[cx-dx,cy-dy],[cx+dx,cy-dy],
            [cx+dx,cy+dy],[cx-dx,cy+dy],[cx-dx,cy-dy]]]}

def png_bytes(w=8, h=8, seed=0):
    # tiny valid PNG generated without third-party libs
    import struct, zlib
    random.seed(seed)
    raw = b""
    for y in range(h):
        raw += b"\x00" + bytes(random.randrange(256) for _ in range(w*3))
    def chk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + chk(b"IHDR", ihdr) + chk(b"IDAT", zlib.compress(raw)) + chk(b"IEND", b"")

def wav_bytes(seconds=1, freq=440, seed=0):
    import math, struct
    fr = 8000; n = fr*seconds
    samples = b"".join(struct.pack("<h", int(8000*math.sin(2*math.pi*freq*i/fr))) for i in range(n))
    h = b"RIFF" + struct.pack("<I", 36+len(samples)) + b"WAVEfmt " + struct.pack("<IHHIIHH",
        16,1,1,fr,fr*2,2,16) + b"data" + struct.pack("<I", len(samples))
    return h + samples

def mp4_like(seed=0):
    return b"\x00\x00\x00\x18ftypmp42" + bytes([seed % 256])*2000

def upload(kind, filename, data, captured_at, flight_id, uid, extra=None, pending_id=None):
    sha = hashlib.sha256(data).hexdigest()
    csz = 4096
    body = {"client_uid": uid, "kind": kind, "filename": filename, "mime":
            {"image":"image/png","audio":"audio/wav","video":"video/mp4"}[kind],
            "total_size": len(data), "chunk_size": csz, "sha256": sha,
            "captured_at": captured_at, "flight_id": flight_id}
    if extra: body.update(extra)
    if pending_id: body["pending_id"] = pending_id
    s = req("POST", "/api/uploads", body)
    if s["instant"]:
        print("  instant dedupe:", filename)
        return s["media_id"], s["session_id"], True
    buf = io.BytesIO(data); idx = 0
    while True:
        chunk = buf.read(csz)
        if not chunk: break
        cs = hashlib.sha256(chunk).hexdigest()
        r = req("POST", "/api/uploads/%d/chunks/%d" % (s["session_id"], idx),
                raw=chunk, headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":cs})
        idx += 1
    # 重传第一个块 -> 验证块级幂等去重
    dup = req("POST", "/api/uploads/%d/chunks/0" % s["session_id"], raw=data[:csz],
              headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":hashlib.sha256(data[:csz]).hexdigest()})
    assert dup["dedup"], "chunk dedup failed"
    return s["media_id"], s["session_id"], False

def main():
    state = req("GET","/api/state")
    if state["plots"]:
        print("already seeded"); return

    # ---- plots (center near Hangzhou farmland) ----
    LNG0, LAT0 = 120.0, 30.0
    req("POST","/api/plots",{"code":"P-A","name":"东一号田","boundary_geojson":box(LNG0, LAT0, 0.0020, 0.0015),
        "area_mu": 52.0, "source":"RTK 打点", "rms_error_m":0.3})
    req("POST","/api/plots",{"code":"P-B","name":"西二号田","boundary_geojson":box(LNG0-0.0050, LAT0, 0.0018,0.0015),
        "area_mu": 41.0, "source":"无人机航测", "rms_error_m":1.2})
    req("POST","/api/plots",{"code":"P-C","name":"南三号田","boundary_geojson":box(LNG0, LAT0-0.0042, 0.0016,0.0012),
        "area_mu": 26.0, "source":"手绘估界", "rms_error_m":8.0})

    # ---- flights: flight_date vs takeoff/capture time separate ----
    f1 = req("POST","/api/flights",{"code":"FL-001","drone_model":"M30M","camera_model":"Zenmuse H20T",
        "pilot":"飞手甲","flight_date":"2026-09-20","takeoff_at":"2026-09-20T09:02:00",
        "note":"首轮巡检"})["flight_id"]
    f2 = req("POST","/api/flights",{"code":"FL-002","drone_model":"M30M","camera_model":"Zenmuse H20T",
        "pilot":"飞手甲","flight_date":"2026-09-28","takeoff_at":"2026-09-28T16:20:00",
        "note":"复检(雨后)"})["flight_id"]

    # ---- media: captured 09-20 but uploaded 09-22 (三时间分离) ----
    img1 = png_bytes(64,64,1)
    m_img1,_,_ = upload("image","DJI_0001.png",img1,"2026-09-20T09:12:31",f1,"uid-img1")
    img2 = png_bytes(64,64,2)
    m_img2,_,_ = upload("image","DJI_0188.png",img2,"2026-09-20T09:41:05",f1,"uid-img2")
    img3 = png_bytes(64,64,3)
    m_img3,_,_ = upload("image","DJI_0201_r2.png",img3,"2026-09-28T16:35:50",f2,"uid-img3")
    aud = wav_bytes(1, seed=4)
    m_aud,_,_ = upload("audio","VOICE_0912.wav",aud,"2026-09-20T09:15:02",f1,"uid-aud1")
    vid = mp4_like(5)
    m_vid,_,_ = upload("video","DJI_VID_0032.mp4",vid,"2026-09-20T10:02:00",f1,"uid-vid1",
                       extra={"duration_ms":46000})
    # 整文件重复上传(同一 sha256) -> 秒传
    _,_,instant = upload("image","DJI_0001_copy.png",img1,"2026-09-20T09:12:31",f1,"uid-img1-dup")
    assert instant, "instant dedupe failed"

    # ---- video clip: 源时间码保留, 模拟“裁切改变时间码” ----
    clip = req("POST",f"/api/media/{m_vid}/clips",{"src_start_ms":12000,"src_end_ms":18500,
        "note":"疑似缺苗片段(第2版裁切, 片段本地时间码00:00需映射回源)"})
    cid = clip["clip_id"]

    # ---- evidence: 镜头画面坐标 + 位姿 -> 地理坐标(严格投影) ----
    # 发现1: P-A 内, nadir
    ev1 = req("POST","/api/evidence",{"media_id":m_img1,"frame_cx":0.51,"frame_cy":0.48,
        "fov_h":73,"fov_v":53,"drone_lng":120.0004,"drone_lat":30.0003,"drone_alt":120,
        "ground_elevation":6,"yaw":35,"pitch":-90,"dem_source":"srtm","drone_hrms":2.5,
        "camera_model":"Zenmuse H20T"})
    # 发现2(同一块地, 第二轮复检): 斜视
    ev2 = req("POST","/api/evidence",{"media_id":m_img3,"frame_cx":0.30,"frame_cy":0.62,
        "fov_h":73,"fov_v":53,"drone_lng":120.0008,"drone_lat":30.0006,"drone_alt":120,
        "ground_elevation":6,"yaw":210,"pitch":-60,"dem_source":"dsm_local","drone_hrms":1.5,
        "camera_model":"Zenmuse H20T"})
    # 语音证据
    ev3 = req("POST","/api/evidence",{"media_id":m_aud,"frame_cx":0.5,"frame_cy":0.5,
        "fov_h":73,"fov_v":53,"drone_lng":119.9999,"drone_lat":30.0002,"drone_alt":80,
        "ground_elevation":6,"yaw":0,"pitch":-90,"dem_source":"srtm","drone_hrms":4.0})
    # 视频帧证据: frame_time 用源时间码; 帧内坐标
    ev4 = req("POST","/api/evidence",{"media_id":m_vid,"clip_id":cid,"frame_time_ms":15200,
        "frame_cx":0.5,"frame_cy":0.5,"fov_h":73,"fov_v":53,"drone_lng":120.0010,"drone_lat":30.0000,
        "drone_alt":120,"ground_elevation":6,"yaw":90,"pitch":-90,"dem_source":"srtm","drone_hrms":2.5})
    # 另一个发现(独立事件, 后续触发冲突): 与发现1接近
    ev5 = req("POST","/api/evidence",{"media_id":m_img2,"frame_cx":0.5,"frame_cy":0.5,
        "fov_h":73,"fov_v":53,"drone_lng":120.00042,"drone_lat":30.00031,"drone_alt":120,
        "ground_elevation":6,"yaw":35,"pitch":-90,"dem_source":"srtm","drone_hrms":2.5})

    # ---- findings (定位依据+误差必填) ----
    fnd1 = req("POST","/api/findings",{"title":"疑似缺苗斑块","ftype":"缺苗","severity":"中",
        "location_lng":ev1["center_lng"],"location_lat":ev1["center_lat"],
        "locate_basis":"正射照片帧中心+RTK位姿+SRTM, footprint 投影, 误差圆",
        "locate_error_m":ev1["horizontal_error_m"],"flight_id":f1,
        "first_seen_at":"2026-09-20T09:12:31","evidence_ids":[ev1["evidence_id"],ev3["evidence_id"],ev4["evidence_id"]]})
    fid1 = fnd1["id"]
    # 第二次巡检证据(同一发现, 不同轮次, 不得因图像相似合并成一次事件)
    req("POST",f"/api/findings/{fid1}/inspections",{"flight_id":f2,
        "inspected_at":"2026-09-28T16:35:50","evidence_ids":[ev2["evidence_id"]],
        "note":"雨后复检, 缺苗范围扩大"})
    fnd2 = req("POST","/api/findings",{"title":"叶斑疑似(另一处)","ftype":"病害","severity":"低",
        "location_lng":ev5["center_lng"],"location_lat":ev5["center_lat"],
        "locate_basis":"相邻航带照片帧中心投影, 距缺苗点约数米, 需人工确认是否同一事件",
        "locate_error_m":ev5["horizontal_error_m"]+2,"flight_id":f1,
        "first_seen_at":"2026-09-20T09:41:05","evidence_ids":[ev5["evidence_id"]]})
    fid2 = fnd2["id"]

    # ---- 待补证据状态: 先建发现, 素材后补(占位附件) ----
    p1 = req("POST","/api/pending",{"client_uid":"pend-1","owner_device":"dev-B","kind":"image",
        "filename":"IMG_pending.jpg","note":"现场补拍(断网未传)","finding_id":fid2})

    # ---- 审核 + 用户建议 ----
    req("POST",f"/api/findings/{fid1}/suggestions",{"content":"建议沿缺苗斑块做人工查苗, 再决定是否补播"})
    req("POST",f"/api/findings/{fid1}/review",{})

    # ---- 生成预览包(仅 reviewed + 用户建议) ----
    rep = req("POST","/api/reports/generate",{"title":"2026年9月航拍巡检预览"})
    rid = rep["report_id"]
    # 派出复核(保留报告快照引用)
    req("POST",f"/api/reports/{rid}/dispatch",{"finding_ids":[fid1],"assigned_to":2,
        "note":"请现场复核缺苗面积","due_at":"2026-10-06"})
    # 导出(应成功, 全部对象在)
    ex1 = req("POST",f"/api/reports/{rid}/export",{})
    # 制造“导出中素材缺失”: 物理删除一个对象再导出
    import db as D
    con = D.connect()
    m = con.execute("SELECT object_key FROM media WHERE id=?", (m_img1,)).fetchone()
    con.close()
    os.remove(D.object_path(m["object_key"]))
    con = D.connect()
    con.execute("UPDATE media SET status='missing' WHERE object_key=?", (m["object_key"],))
    con.commit(); con.close()
    ex2 = req("POST",f"/api/reports/{rid}/export",{})

    # ---- 边界修订: P-A 向北扩 0.0012, 触发发现重判(演示; 现有点仍在内) ----
    rev = req("POST","/api/plots/1/revisions",{"boundary_geojson":box(120.0,30.0008,0.0020,0.0023),
        "area_mu":61.0,"source":"RTK 复核","rms_error_m":0.2,"reason":"按田埂实测北扩"})

    print(json.dumps({"fid1":fid1,"fid2":fid2,"clip":cid,"report":rid,
        "export_ok":ex1["status"],"export_missing":ex2["status"],
        "missing_count":len(ex2["missing"]),"rejudged":rev["rejudged"]}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
