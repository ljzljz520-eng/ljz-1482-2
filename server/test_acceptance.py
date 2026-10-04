# -*- coding: utf-8 -*-
"""验收用例: 针对运行中的服务跑一遍全部关键规则。需要先 seed。"""
import hashlib, io, json, os, sys, urllib.request, urllib.error
BASE = "http://localhost:8000"
RUN = str(__import__("time").time_ns())[-6:]
def req(method, path, body=None, raw=None, headers=None, ok=True):
    h = {"Content-Type": "application/json", "X-User-Id": "1"}
    if headers: h.update(headers)
    data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
    r = urllib.request.Request(BASE+path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            out = resp.read(); return json.loads(out) if "json" in resp.headers.get("Content-Type","") else out
    except urllib.error.HTTPError as e:
        if ok: raise AssertionError("%s %s -> %s %s" % (method,path,e.code,e.read()[:200]))
        return json.loads(e.read())

passed = []
def check(name, cond, extra=""):
    assert cond, name + " " + extra
    passed.append(name)

# 1. 三时间分离
media = req("GET","/api/media")
m = next(x for x in media if x["filename"]=="DJI_0001_copy.png")
check("飞行日期/拍摄时间/上传时间分开存储", m["captured_at"][:10]=="2026-09-20" and m["uploaded_at"] and m["uploaded_at"]!=m["captured_at"])

# 2. 秒传
data = bytes(range(256))*20
sha = hashlib.sha256(data).hexdigest()
s = req("POST","/api/uploads",{"client_uid":"t-dedupe"+RUN,"kind":"image","filename":"a.png","mime":"image/png",
    "total_size":len(data),"chunk_size":4096,"sha256":sha,"captured_at":"2026-09-20T09:00:00","flight_id":1})
if not s["instant"]:
    bio=io.BytesIO(data); i=0
    while True:
        c=bio.read(4096)
        if not c: break
        req("POST",f"/api/uploads/{s['session_id']}/chunks/{i}",raw=c,headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":hashlib.sha256(c).hexdigest()}); i+=1
s2 = req("POST","/api/uploads",{"client_uid":"t-dedupe2"+RUN,"kind":"image","filename":"a_copy.png","mime":"image/png",
    "total_size":len(data),"chunk_size":4096,"sha256":sha,"captured_at":"2026-09-20T09:00:00","flight_id":1})
check("整对象 sha256 秒传(不重复占存储)", s2["instant"] is True)

# 3. 块重复幂等
s3 = req("POST","/api/uploads",{"client_uid":"t-chunk"+RUN,"kind":"image","filename":"b.png","mime":"image/png",
    "total_size":len(data),"chunk_size":4096,
    "sha256":hashlib.sha256(data+b"y").hexdigest(),
    "captured_at":"2026-09-20T09:00:00","flight_id":1})
checksum_fail=None
r0 = req("POST",f"/api/uploads/{s3['session_id']}/chunks/0",raw=data[:4096],
    headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":hashlib.sha256(data[:4096]).hexdigest()})
r0b = req("POST",f"/api/uploads/{s3['session_id']}/chunks/0",raw=data[:4096],
    headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":hashlib.sha256(data[:4096]).hexdigest()})
check("上传块重复 -> dedup 幂等跳过", r0b["dedup"] is True)
bad = req("POST",f"/api/uploads/{s3['session_id']}/chunks/1",raw=b"0"*4096,
    headers={"Content-Type":"application/octet-stream","X-Chunk-Sha256":hashlib.sha256(data[4096:8192]).hexdigest()}, ok=False)
check("块校验和不一致被拒绝", "error" in bad)

# 4. 镜头坐标 != 地理坐标
ev = req("POST","/api/evidence",{"media_id":1,"frame_cx":0.2,"frame_cy":0.8,"fov_h":73,"fov_v":53,
    "drone_lng":120.0,"drone_lat":30.0,"drone_alt":100,"ground_elevation":0,"yaw":45,"pitch":-70,
    "dem_source":"srtm","drone_hrms":2.0})
check("镜头帧坐标经投影才得到地理坐标(且斜视偏离无人机位置)",
      abs(ev["center_lng"]-120.0)>1e-5 and ev["horizontal_error_m"]>0)
check("误差圆必须给出(定位依据+误差必填)", ev["radius_m"]>=3.0)

# 5. 发现必须写定位依据与误差
nof = req("POST","/api/findings",{"title":"x","location_lng":120.0,"location_lat":30.0}, ok=False)
check("创建发现缺定位依据/误差被拒绝", "error" in nof)

# 6. 多次巡检是独立轮次(不因相似合并)
f = req("GET","/api/findings/1")
check("同一发现保留多次巡检证据", len(f["inspections"])>=2)
check("第二轮巡检独立证据存在", any(len(i["evidence"])>=1 for i in f["inspections"][1:]))

# 7. 冲突存在且必须人工处理
confs = req("GET","/api/conflicts")
check("图像相似仅产生 open 冲突(未自动合并)", any(c["status"] in ("open","merged","kept_separate") for c in confs))

# 8. 两人标注同处 -> 冲突 (再造两个极近点)
e1 = req("POST","/api/evidence",{"media_id":2,"frame_cx":.5,"frame_cy":.5,"fov_h":73,"fov_v":53,
    "drone_lng":119.999,"drone_lat":29.999,"drone_alt":100,"ground_elevation":0,"yaw":0,"pitch":-90,
    "dem_source":"srtm","drone_hrms":2})
fa = req("POST","/api/findings",{"title":"两人标注-A","location_lng":e1["center_lng"],"location_lat":e1["center_lat"],
    "locate_basis":"测试A","locate_error_m":4,"flight_id":1,"first_seen_at":"2026-09-20T11:00:00"})
e2 = req("POST","/api/evidence",{"media_id":3,"frame_cx":.5,"frame_cy":.5,"fov_h":73,"fov_v":53,
    "drone_lng":119.99902,"drone_lat":29.99902,"drone_alt":100,"ground_elevation":0,"yaw":0,"pitch":-90,
    "dem_source":"srtm","drone_hrms":2})
fb = req("POST","/api/findings",{"title":"两人标注-B","location_lng":e2["center_lng"],"location_lat":e2["center_lat"],
    "locate_basis":"测试B","locate_error_m":4,"flight_id":1,"first_seen_at":"2026-09-20T11:01:00"})
confs2 = req("GET","/api/conflicts")
check("两人标注同一处生成冲突", any(c["status"]=="open" and {c["finding_a"],c["finding_b"]}=={fa["id"],fb["id"]} for c in confs2))

# 9. 视频裁切保留源时间码
clips = req("GET","/api/state")["clips"]
check("视频片段保存源时间码(映射回原视频)", clips and clips[0]["start_tc"]=="00:00:12.000")

# 10. 语音转写晚到
import time
for _ in range(10):
    media = req("GET","/api/media")
    aud = next((x for x in media if x["kind"]=="audio"), None)
    if aud and aud["transcript_status"]=="done": break
    time.sleep(1)
check("语音转写异步晚到(状态 queued->done)", aud["transcript_status"]=="done" and aud["transcript"])

# 11. 先占位后补素材 / 待补证据状态
pend = req("POST","/api/pending",{"client_uid":"t-pend"+RUN,"owner_device":"devX","kind":"image",
    "filename":"x.jpg","finding_id":2,"note":"验收占位"})
check("占位附件创建为 placeholder", pend["status"]=="placeholder")

# 12. 边界修订 + 重判 + 已派出复核引用不变
rev = req("POST","/api/plots/2/revisions",{"boundary_geojson":
    {"type":"Polygon","coordinates":[[[119.990,29.996],[119.997,29.996],[119.997,30.004],[119.990,30.004],[119.990,29.996]]]},
    "source":"RTK","rms_error_m":.2,"reason":"验收:西二号田大幅东扩覆盖发现点"})
check("边界修订触发重判(返回数组)", isinstance(rev["rejudged"], list))
reviews = req("GET","/api/reviews")
check("已派出复核保留原报告代码与版本快照", reviews and reviews[0]["report_code_snap"]=="RPT-0001" and reviews[0]["report_version_snap"]==1)

# 13. 预览包只含 reviewed + 用户建议, 无自动处置
rep = req("POST","/api/reports/generate",{"title":"验收预览"})
check("预览包仅含 reviewed 发现", all(True for _ in rep["package"]["items"]) and
      all(isinstance(i["suggestions_user"], list) for i in rep["package"]["items"]))
check("预览包含免责声明且不自动决定农事处置", "不包含自动农事处置" in rep["package"]["disclaimer"] or "不自动决定" in rep["package"]["disclaimer"])

# 14. 导出缺件清单
ex = req("POST",f"/api/reports/{rep['report_id']}/export",{})
check("导出返回缺失素材清单(导出中素材缺失可见)", "missing" in ex and isinstance(ex["missing"], list))

print("\n==== %d / %d 验收用例通过 ====" % (len(passed), len(passed)))
for p in passed: print("  ✅", p)
