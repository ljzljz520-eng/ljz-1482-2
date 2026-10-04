# -*- coding: utf-8 -*-
"""
端到端验收测试 (纯标准库): 自启服务(临时库/临时端口), 覆盖:
 A 上传块重复(幂等/冲突) + 断网续传 + 跨设备补充; 三类时间分离
 B 语音转写晚到
 C 视频裁切改变时间码
 D 两人标注同处 -> 冲突 -> 合并/保留; 不按图像相似自动合并
 E 导出中素材缺失 -> 待补; 补齐后导出成功
 另含: 像素坐标禁当地理坐标、边界修订重判归属、复核保留原报告引用、
 预览包只含已审核发现+用户建议。
"""
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

PORT = 8091
BASE = "http://127.0.0.1:%d" % PORT
PASS, FAILS = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAILS.append((name, detail))
        print("  FAIL", name, detail)


def req(method, path, body=None, raw=None, headers=None, expect=None):
    url = BASE + path
    data = None
    h = {"Content-Type": "application/json"}
    if raw is not None:
        data = raw
        h["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return resp.status, json.loads(payload.decode())
            return resp.status, payload
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def wait_port():
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="wb-accept-")
    env = dict(os.environ, PORT=str(PORT),
               WORKBENCH_DB=os.path.join(tmp, "test.db"))
    proc = subprocess.Popen([sys.executable, "app.py"],
                            cwd=os.path.dirname(os.path.abspath(__file__)),
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        if not wait_port():
            print("server failed to start")
            return 1
        run_tests()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
    print("\n%d passed, %d failed" % (PASS, len(FAILS)))
    for n, d in FAILS:
        print(" -", n, d)
    return 1 if FAILS else 0


def run_tests():
    dev1 = req("POST", "/api/devices", {"name": "巡检手机1", "platform": "ios"})[1]["device_id"]
    dev2 = req("POST", "/api/devices", {"name": "巡检手机2", "platform": "android"})[1]["device_id"]

    wb = req("GET", "/api/workbench")[1]
    plot = wb["plots"][0]
    flight = wb["flights"][0]["id"]

    print("\n[A] 分块上传: 重复块幂等 / 异 sha 冲突 / 断网续传 / 跨设备补充")
    content = bytes((i * 7) % 256 for i in range(7000))
    chunks = [content[0:2500], content[2500:5000], content[5000:7000]]
    st, a = req("POST", "/api/assets", {
        "kind": "image", "filename": "field.jpg", "flight_id": flight,
        "device_id": dev1, "capture_time": 1780502400000,
        "total_chunks": 3, "total_size": len(content),
        "whole_sha256": hashlib.sha256(content).hexdigest()})
    check("create asset+session", st == 200 and "resume_token" in a, a)
    token = a["resume_token"]; aid = a["asset_id"]

    st, r = req("PUT", "/api/uploads/%s/chunks/0" % token, raw=chunks[0],
                headers={"X-Device-Id": dev1})
    check("chunk0 first -> 200 not-duplicate", st == 200 and r["duplicate"] is False, (st, r))
    st, r = req("PUT", "/api/uploads/%s/chunks/0" % token, raw=chunks[0],
                headers={"X-Device-Id": dev1})
    check("chunk0 duplicate identical -> 200 duplicate", st == 200 and r["duplicate"] is True, (st, r))
    st, r = req("PUT", "/api/uploads/%s/chunks/0" % token, raw=b"x" * 2500,
                headers={"X-Device-Id": dev1})
    check("chunk0 same-index different sha -> 409", st == 409 and r.get("error") == "chunk_conflict", (st, r))

    # 模拟断网: 另一台设备凭 resume_token 查询进度并续传剩余块
    st, sess = req("GET", "/api/uploads/%s" % token)
    check("resume query shows 1 received", set(sess["received_chunks"]) == {"0"}, sess)
    st, r = req("PUT", "/api/uploads/%s/chunks/1" % token, raw=chunks[1],
                headers={"X-Device-Id": dev2})
    check("cross-device chunk1 -> 200", st == 200, (st, r))
    st, r = req("PUT", "/api/uploads/%s/chunks/2" % token, raw=chunks[2],
                headers={"X-Device-Id": dev2})
    check("cross-device chunk2 -> 200", st == 200, (st, r))
    st, r = req("POST", "/api/uploads/%s/complete" % token, {})
    check("complete -> sha matches", st == 200 and
          r["sha256"] == hashlib.sha256(content).hexdigest(), (st, r))
    detail = req("GET", "/api/assets/%s" % aid)[1]
    check("upload_time set server-side", detail["upload_time"] and
          detail["upload_time"] != detail["capture_time"],
          (detail.get("capture_time"), detail.get("upload_time")))
    check("status complete", detail["status"] == "complete", detail["status"])

    print("\n[B] 语音转写晚到: 素材先 complete, 转写 pending, 之后异步到达")
    voice = b"AUDIO" * 2000
    _, va = req("POST", "/api/assets", {
        "kind": "audio", "filename": "note.m4a", "flight_id": flight,
        "device_id": dev1, "capture_time": 1780503000000,
        "total_chunks": 1, "total_size": len(voice),
        "whole_sha256": hashlib.sha256(voice).hexdigest()})
    req("PUT", "/api/uploads/%s/chunks/0" % va["resume_token"], raw=voice)
    req("POST", "/api/uploads/%s/complete" % va["resume_token"], {})
    st, t = req("POST", "/api/assets/%s/transcription" % va["asset_id"], {"status": "ordered"})
    check("transcription ordered, pending", st == 200 and "transcription_id" in t, (st, t))
    ad = req("GET", "/api/assets/%s" % va["asset_id"])[1]
    check("transcription pending initially",
          ad["transcriptions"][0]["status"] == "pending", ad["transcriptions"])
    time.sleep(0.02)
    st, _ = req("POST", "/api/assets/%s/transcription" % va["asset_id"],
                {"text": "东北角垄沟有积水", "provider": "mock-asr"})
    ad = req("GET", "/api/assets/%s" % va["asset_id"])[1]
    tx = ad["transcriptions"][0]
    check("transcription late-ready", tx["status"] == "ready" and
          tx["ready_at"] >= tx["ordered_at"] and tx["text"] == "东北角垄沟有积水", tx)

    print("\n[C] 视频裁切改变时间码 (t'=t-offset), 源时间码保留")
    vid = b"VIDEO" * 9000
    _, vma = req("POST", "/api/assets", {
        "kind": "video", "filename": "clip.mp4", "flight_id": flight,
        "device_id": dev1, "capture_time": 1780503600000, "duration_ms": 90000,
        "total_chunks": 1, "total_size": len(vid),
        "whole_sha256": hashlib.sha256(vid).hexdigest()})
    req("PUT", "/api/uploads/%s/chunks/0" % vma["resume_token"], raw=vid)
    req("POST", "/api/uploads/%s/complete" % vma["resume_token"], {})
    st, seg = req("POST", "/api/assets/%s/segments" % vma["asset_id"],
                  {"source_start_ms": 30000, "source_end_ms": 45000, "name": "特写"})
    check("segment created with offset 30000", st == 200 and
          seg["timecode_offset_ms"] == 30000, seg)

    print("\n[规则] 镜头像素坐标不能直接当地理坐标")
    st, r = req("POST", "/api/findings", {
        "title": "坏数据", "pixel_x": 100, "pixel_y": 200,
        "lon": 116.4, "lat": 39.9})
    check("lon/lat without method+error rejected", st == 400, (st, r))
    st, r = req("POST", "/api/findings", {
        "title": "坏数据2", "lon": 116.4, "lat": 39.9,
        "geo_method": "frame_projection"})
    check("method without error rejected", st == 400, (st, r))
    st, r = req("POST", "/api/findings", {
        "title": "相机投影发现", "camera": {
            "drone_lon": 116.3975, "drone_lat": 39.9085, "drone_alt_m": 100,
            "yaw_deg": 0, "pitch_deg": -90, "focal_px": 800,
            "frame_w": 4000, "frame_h": 3000, "px": 2000, "py": 1500,
            "gnss_error_m": 3, "attitude_error_deg": 2}})
    check("frame_projection accepted with computed error", st == 200, (st, r))
    fid_proj = r.get("finding_id")

    print("\n[D] 两人标注同处 -> 冲突; 保留双标; 合并不自动发生")
    # 找种子里的同事件
    ev2 = req("POST", "/api/events", {"flight_id": wb["flights"][-1]["id"],
        "event_date": "2026-10-02", "kind": "ground",
        "inspector": "test", "note": "验收测试专用事件"})[1]["event_id"]
    st, a1 = req("POST", "/api/annotations", {
        "finding_id": fid_proj, "event_id": ev2,
        "lon": 116.3976, "lat": 39.9086, "geo_method": "manual_pin",
        "geo_error_m": 5, "label": "中心"}, headers={"X-Actor": "annotator_jia"})
    check("annotation 1", st == 200, (st, a1))
    # 偏移约 8m 处, 第二人标注
    _, p0 = req("GET", "/api/plots")
    import math
    mlon = 6378137 * math.radians(1) * math.cos(math.radians(39.9086))
    st, a2 = req("POST", "/api/annotations", {
        "finding_id": fid_proj, "event_id": ev2,
        "lon": 116.3976 + 8 / mlon, "lat": 39.9086,
        "geo_method": "manual_pin", "geo_error_m": 5,
        "label": "中心?"}, headers={"X-Actor": "annotator_yi"})
    check("annotation 2 raises conflict", st == 200 and len(a2.get("conflicts", [])) == 1, a2)
    cid = a2["conflicts"][0]
    cf = [c for c in req("GET", "/api/conflicts")[1]["conflicts"] if c["id"] == cid][0]
    check("conflict open, distance ~8m, both authors",
          cf["status"] == "open" and 5 < cf["distance_m"] < 12 and
          cf["ref_a_detail"]["author"] != cf["ref_b_detail"]["author"], cf)
    st, _ = req("POST", "/api/conflicts/%s/resolve" % cid,
                {"resolution": "keep_both", "note": "保留各自标注待核"},
                headers={"X-Actor": "supervisor"})
    check("resolve keep_both -> 200", st == 200, st)
    cf2 = [c for c in req("GET", "/api/conflicts")[1]["conflicts"] if c["id"] == cid][0]
    check("conflict resolved, both annotations retained",
          cf2["status"] == "resolved_keep_both" and cf2["ref_a_detail"] and cf2["ref_b_detail"], cf2)

    # 两个不同发现即使图像证据相同, 也绝不自动合并; 只能人工提议并决策
    findings = req("GET", "/api/findings")[1]["findings"]
    f_titles = {f["title"]: f["id"] for f in findings}
    fs = [f for f in findings if f["status"] in ("pending_review", "reviewed")]
    src, tgt = fs[0]["id"], fs[1]["id"]
    st, mp = req("POST", "/api/merge-proposals", {
        "source_finding_id": src, "target_finding_id": tgt,
        "reason": "人工判断同一位置(非图像相似自动合并)"}, headers={"X-Actor": "supervisor"})
    check("manual merge proposal created", st == 200, (st, mp))
    st, _ = req("POST", "/api/merge-proposals/%s/decide" % mp["proposal_id"],
                {"accept": False}, headers={"X-Actor": "supervisor"})
    check("reject merge -> both findings remain", st == 200)
    f_after = {f["id"]: f for f in req("GET", "/api/findings")[1]["findings"]}
    check("source finding still present after rejection",
          src in f_after and f_after[src]["status"] != "disputed",
          f_after.get(src))

    print("\n[模式B+跨设备] 先建占位附件 -> 挂发现(needs_evidence) -> 草稿码跨设备补充")
    _, pa = req("POST", "/api/assets", {
        "kind": "image", "filename": "later.jpg", "flight_id": flight,
        "capture_time": 1780504200000, "placeholder": True})
    check("placeholder asset pending", pa["asset_id"] and req(
        "GET", "/api/assets/%s" % pa["asset_id"])[1]["status"] == "pending_placeholder")
    _, pf = req("POST", "/api/findings", {
        "title": "占位待补发现", "category": "草害",
        "lon": 116.3975, "lat": 39.9085, "geo_method": "manual_pin",
        "geo_error_m": 6, "evidences": [{"asset_id": pa["asset_id"], "note": "待补"}]})
    check("finding with placeholder stays needs_evidence",
          req("GET", "/api/findings/%s" % pf["finding_id"])[1]["status"] == "needs_evidence")
    st, dr = req("POST", "/api/drafts", {
        "device_id": dev1, "resume_tokens": [],
        "payload": {"note": "回家有 wifi 再传", "asset_id": pa["asset_id"]}})
    code = dr["share_code"]
    st, fetched = req("GET", "/api/drafts/%s" % code)
    check("cross-device draft fetch by share code", st == 200 and
          fetched["payload"]["asset_id"] == pa["asset_id"], (st, fetched))

    print("\n[E] 导出中素材缺失 -> failed+missing; 补齐后 -> complete")
    rep = req("POST", "/api/reports", {"title": "验收临时报告"})[1]["report_id"]
    req("POST", "/api/reports/%s/findings" % rep,
        {"finding_ids": [pf["finding_id"], fid_proj]})
    st, job = req("POST", "/api/reports/%s/export" % rep, {})
    check("export fails with missing items", st == 200 and job["ok"] is False and
          len(job["missing"]) == 1 and job["missing"][0]["asset_id"] == pa["asset_id"], job)
    st, jd = req("GET", "/api/exports/%s" % job["export_job_id"])
    check("export job recorded failed", jd["status"] == "failed" and
          len(jd["missing_items"]) == 1, jd)
    # 补齐占位附件: 在第二台设备上为同一个占位 asset 开启会话 -> 传块 -> 完成
    later = b"LATER-IMAGE" * 500
    st, us = req("POST", "/api/assets/%s/upload-session" % pa["asset_id"], {
        "total_chunks": 1, "total_size": len(later),
        "whole_sha256": hashlib.sha256(later).hexdigest()},
        headers={"X-Device-Id": dev2})
    check("placeholder asset gets upload session (cross-device)",
          st == 200 and us["resume_token"], (st, us))
    req("PUT", "/api/uploads/%s/chunks/0" % us["resume_token"], raw=later,
        headers={"X-Device-Id": dev2})
    st, done = req("POST", "/api/uploads/%s/complete" % us["resume_token"], {})
    check("placeholder upload completes same asset",
          st == 200 and done["asset_id"] == pa["asset_id"], (st, done))
    check("finding auto-progresses after placeholder filled",
          req("GET", "/api/findings/%s" % pf["finding_id"])[1]["status"] == "pending_review")
    # 占位证据已补齐 -> 报告导出不再报缺素材 (但发现尚未审核; 导出不限制审核状态)
    st, job2 = req("POST", "/api/reports/%s/export" % rep, {})
    check("export now succeeds for filled report", job2["ok"], job2)
    # 反例: 标记另一个素材 missing, 导出必须检出
    req("POST", "/api/assets/%s/missing" % pa["asset_id"], {})
    st, jobx = req("POST", "/api/reports/%s/export" % rep, {})
    check("export detects object status missing",
          jobx["ok"] is False and any(
              m["asset_id"] == pa["asset_id"] for m in jobx["missing"]), jobx)
    # 恢复后用仅含已审核投影发现的报告做成功导出
    req("PUT", "/api/uploads/%s/chunks/0" % us["resume_token"], raw=later,
        headers={"X-Device-Id": dev2})
    req("POST", "/api/findings/%s/status" % fid_proj,
        {"status": "reviewed", "note": "验收审核"})
    req("POST", "/api/findings/%s/evidence" % fid_proj,
        {"asset_id": aid, "pixel_x": 2000, "pixel_y": 1500,
         "frame_width": 4000, "frame_height": 3000, "note": "验收证据(像素坐标仅画面刺点)"})
    rep2 = req("POST", "/api/reports", {"title": "验收导出报告"})[1]["report_id"]
    req("POST", "/api/reports/%s/findings" % rep2, {"finding_ids": [fid_proj]})
    st, job3 = req("POST", "/api/reports/%s/export" % rep2, {})
    check("export succeeds when media present", st == 200 and job3["ok"], job3)
    st, zbytes = req("GET", "/api/exports/%s?download=1" % job3["export_job_id"])
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = zf.namelist()
    check("zip has manifest+media", "manifest.json" in names and
          any(n.startswith("media/") for n in names), names)

    print("\n[规则] 边界修订 -> 归属重判留痕; 已派出复核保留原报告引用")
    # 取种子 auto_buffer 的发现(边界处积水痕迹)
    findings = req("GET", "/api/findings")[1]["findings"]
    buf = [f for f in findings if f["attribution_status"] == "auto_buffer"][0]
    tasks_before = req("GET", "/api/review-tasks")[1]["tasks"]
    check("seed review task keeps original report link",
          all(t["original_report_id"] for t in tasks_before), tasks_before)
    orig_ref = tasks_before[0]["original_report_id"]
    # 把地块边界整体向东扩 0.001 度(约85m), 使原 buffer 点变为 inside
    geom = plot["geometry"]
    ring = geom["coordinates"][0]
    new_ring = [[x + 0.001, y] for x, y in ring]
    st, rv = req("POST", "/api/plots/%s/revisions" % plot["id"],
                 {"geometry": {"type": "Polygon", "coordinates": [new_ring]},
                  "change_note": "东边界外扩约60m"})
    check("new immutable version created", st == 200 and rv["version"] == plot["current_version"] + 1, rv)
    fd = req("GET", "/api/findings/%s" % buf["id"])[1]
    check("buffer finding reattributed inside",
          fd["attribution_status"] == "auto_inside" and
          len(fd["attribution_history"]) >= 1,
          (fd["attribution_status"], fd["attribution_history"]))
    tasks_after = req("GET", "/api/review-tasks")[1]["tasks"]
    t0 = [t for t in tasks_after if t["original_report_id"] == orig_ref]
    check("dispatched review task retains original report reference",
          bool(t0), tasks_after)
    check("plot now has 2 versions",
          len(req("GET", "/api/plots/%s" % plot["id"])[1]["versions"]) == 2)

    print("\n[规则] 预览包: 只含 reviewed + 用户建议, 不含自动农事处置")
    # 种子报告
    seed_report = [r for r in req("GET", "/api/reports")[1]["reports"]
                   if "东三块" in r["title"]][0]
    st, pv = req("POST", "/api/reports/%s/preview" % seed_report["id"], {})
    check("preview built; unreviewed excluded", st == 200 and
          pv["included"] >= 1 and pv["excluded"] >= 1, pv)
    st, zbytes = req("GET", "/api/preview/%s" % pv["preview_id"])
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    manifest = json.loads(zf.read("manifest.json").decode())
    check("manifest only reviewed", all(f["status"] == "reviewed"
                                        for f in manifest["findings"]),
          [f["status"] for f in manifest["findings"]])
    check("user suggestions carried", len(manifest["user_suggestions"]) >= 1 and
          "三唑酮" in manifest["user_suggestions"][0]["suggestion"],
          manifest["user_suggestions"])
    readme = zf.read("README.txt").decode()
    check("README states no automated decision", "不构成农事处置决定" in readme, readme)
    html = zf.read("preview.html").decode()
    check("html contains user suggestion text", "三唑酮" in html, "")
    check("html contains no system decision wording",
          "系统建议" not in html and "自动处置" not in html, "")

    print("\n[规则] 审核门禁: 无完整素材证据不能 reviewed")
    st, r = req("POST", "/api/findings/%s/status" % pf["finding_id"],
                {"status": "reviewed"})
    check("review blocked until complete evidence", st == 400, (st, r))


if __name__ == "__main__":
    sys.exit(main())
