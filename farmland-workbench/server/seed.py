# -*- coding: utf-8 -*-
"""首次启动时种入演示数据 (围绕一块 240m 见方的示例农田)。"""
import hashlib
import json
import os
import sys

import geo
import services
import storage
import uploads
from db import db, init, now_ms

C = {"lon": 116.3975, "lat": 39.9085}  # 农田中心


def _offset(lon0, lat0, east_m, north_m):
    mlat, mlon = geo.meters_per_degree(lat0)
    return lon0 + east_m / mlon, lat0 + north_m / mlat


def _square(cx, cy, half_m):
    pts = [_offset(cx, cy, x, y) for (x, y) in
           [(-half_m, -half_m), (half_m, -half_m), (half_m, half_m),
            (-half_m, half_m), (-half_m, -half_m)]]
    return {"type": "Polygon", "coordinates": [pts]}


def _blob(seed, n=4096):
    return (b"FARMLAND-DEMO-BLOB:" + seed.encode() + b":" + os.urandom(n))[:n if n > 32 else n]


def seed_if_empty(force=False):
    init()
    storage.init()
    if db().execute("SELECT COUNT(*) c FROM plots").fetchone()["c"] > 0 and not force:
        return False

    # 设备
    drone = services.register_device("大疆 Mavic (演示)", "drone")
    phone = services.register_device("田间手机 A", "android")

    # 地块
    plot = services.create_plot("东三块-冬小麦", "冬小麦", _square(C["lon"], C["lat"], 120),
                                source="import", note="承包地边界导入", actor="seed")

    # 飞行 (飞行日期) 与两次巡检事件
    fl = services.create_flight("2026-09-28", pilot="老王", aircraft="Mavic 3",
                                note="抽穗期巡检", actor="seed")
    ev1 = services.create_event(fl, "2026-09-28", "aerial", inspector="老王",
                                note="第一次航拍")
    fl2 = services.create_flight("2026-09-30", pilot="老王", aircraft="Mavic 3",
                                 note="疑似病斑复飞", actor="seed")
    ev2 = services.create_event(fl2, "2026-09-30", "aerial", inspector="老王",
                                note="第二次航拍核对")

    # ---- 素材 (拍摄时间 != 上传时间) ----
    day1 = 1780502400000  # 2026-09-28T00:00 UTC 附近基准 (演示值)
    capture1 = day1 + 9 * 3600_000 + 12 * 60_000
    upload1 = day1 + 13 * 3600_000

    asset_img = _create_complete_asset(drone, "image", "dji_0001.jpg", fl,
                                       capture1, upload1, _blob("img1"))
    asset_vid = _create_complete_asset(drone, "video", "dji_clip02.mp4", fl,
                                       capture1 + 600_000, upload1 + 300_000,
                                       _blob("vid1", 65536), duration_ms=90_000)
    asset_voice = _create_complete_asset(phone, "audio", "voice_note03.m4a", fl,
                                         capture1 + 620_000, upload1 + 600_000,
                                         _blob("voice1", 16384), duration_ms=18_000)
    # 语音转写: 下单但晚到 (保持 pending, 演示"晚到")
    services.order_transcription(asset_voice)

    # 视频裁切片段 (新时间码 = 源时间码 - 30000)
    services.add_video_segment(asset_vid, 30_000, 45_000, name="病斑特写片段")

    # 第二次巡检的素材 (同一发现的多次巡检证据)
    capture2 = day1 + 2 * 86400_000 + 9 * 3600_000
    upload2 = day1 + 2 * 86400_000 + 11 * 3600_000
    asset_img2 = _create_complete_asset(drone, "image", "dji_0188.jpg", fl2,
                                        capture2, upload2, _blob("img2"))

    # ---- 发现 1: 通过 frame_projection 合法通道定位 ----
    lon, lat, err = geo.frame_projection(
        C["lon"], C["lat"], 100, yaw_deg=0, pitch_deg=-90, roll_deg=0,
        focal_px=800, frame_w=4000, frame_h=3000, px=2100, py=1600)
    f1 = services.create_finding({
        "title": "疑似条锈病斑块", "category": "病害", "severity": "中",
        "lon": lon, "lat": lat, "geo_method": "frame_projection",
        "geo_error_m": err,
        "geo_note": "正射影像 frame_projection, 像素(2100,1600), 像素坐标仅用于画面刺点"},
        actor="老王")
    services.add_evidence(f1, {"event_id": ev1, "asset_id": asset_img,
                               "pixel_x": 2100, "pixel_y": 1600,
                               "frame_width": 4000, "frame_height": 3000,
                               "note": "首飞画面中的浅黄色斑块"}, actor="老王")
    services.add_evidence(f1, {"event_id": ev2, "asset_id": asset_img2,
                               "pixel_x": 900, "pixel_y": 1200,
                               "frame_width": 4000, "frame_height": 3000,
                               "note": "复飞同一位置, 斑块扩大 — 属不同巡检事件"},
                          actor="老王")
    services.add_evidence(f1, {"event_id": ev1, "asset_id": asset_vid,
                               "note": "视频掠过"}, actor="老王")
    services.set_finding_status(f1, "reviewed", actor="植保员小李",
                                note="两次巡检证据一致")

    # ---- 发现 2: manual_pin, 位于边界误差带 -> auto_buffer 待确认 ----
    lon2, lat2 = _offset(C["lon"], C["lat"], 125, -40)
    f2 = services.create_finding({
        "title": "边界处积水痕迹", "category": "水害", "severity": "低",
        "lon": lon2, "lat": lat2, "geo_method": "manual_pin",
        "geo_error_m": 8,
        "geo_note": "人工地图刺点, 手持 GPS 精度 8m; 靠近地块边界"},
        actor="老赵")
    services.add_evidence(f2, {"event_id": ev1, "asset_id": asset_voice,
                               "note": "语音描述: 东北角垄沟有积水"}, actor="老赵")
    # 转写晚到 (取消注释可模拟到达; 默认 pending 以演示等待状态)
    # services.fulfill_transcription(asset_voice, "东北角垄沟有积水，大概两米长")

    # ---- 发现 3: 先建占位附件 (模式 B), 尚未上传 -> needs_evidence ----
    a3 = _placeholder_asset(phone, "image", "pending_weeds.jpg", fl2,
                            capture2 + 3600_000)
    f3 = services.create_finding({
        "title": "猪草富集点 (待补照片)", "category": "草害", "severity": "待评估",
        "lon": C["lon"], "lat": C["lat"], "geo_method": "manual_pin",
        "geo_error_m": 10, "geo_note": "先登记占位, 信号恢复后续传图片"},
        actor="老赵")
    services.add_evidence(f3, {"event_id": ev2, "asset_id": a3,
                               "note": "占位附件, 等待跨设备补充上传"}, actor="老赵")

    # ---- 发现 4: 另一处独立小发现 (图像与 f1 不同, 演示绝不按相似合并) ----
    lon4, lat4 = _offset(C["lon"], C["lat"], -60, 50)
    f4 = services.create_finding({
        "title": "缺苗断垄", "category": "苗情", "severity": "低",
        "lon": lon4, "lat": lat4, "geo_method": "exif_gps",
        "geo_error_m": 3, "geo_note": "照片 EXIF GPS, 设备标称精度 3m"},
        actor="老王")
    services.add_evidence(f4, {"event_id": ev1, "asset_id": asset_img,
                               "pixel_x": 500, "pixel_y": 800,
                               "frame_width": 4000, "frame_height": 3000,
                               "note": "与病斑不同位置; 即使画面相似也不得自动合并"},
                          actor="老王")
    services.set_finding_status(f4, "pending_review", actor="老赵")

    # ---- 两人标注同处 ----
    lon5, lat5 = _offset(C["lon"], C["lat"], 10, 10)
    services.add_annotation({"finding_id": f1, "event_id": ev2,
                             "lon": lon5, "lat": lat5, "geo_method": "manual_pin",
                             "geo_error_m": 5, "label": "病斑中心",
                             "comment": "我刺在这里"}, actor="标注员甲")
    lon5b, lat5b = _offset(lon5, lat5, 6, 0)  # 偏东 6m
    services.add_annotation({"finding_id": f1, "event_id": ev2,
                             "lon": lon5b, "lat": lat5b, "geo_method": "manual_pin",
                             "geo_error_m": 5, "label": "病斑中心?",
                             "comment": "我觉得偏东一点"}, actor="标注员乙")

    # ---- 报告 + 用户建议 + 复核任务 (保留原报告引用) ----
    rp = services.create_report("2026年第39周 东三块巡检报告",
                                "2026-09-27", "2026-10-03", actor="manager")
    services.add_report_findings(rp, [f1, f2, f3, f4], actor="manager")
    services.add_suggestion(f1, rp, "建议选晴天下周叶面喷施三唑酮，剂量按标签",
                            decision="待农户确认", actor="植保员小李")
    services.create_review_task(f1, rp, assignee="老赵",
                                note="地面复核病斑边界并拍照", actor="manager")
    services.publish_report(rp, actor="manager")
    services.build_preview_package(rp, actor="manager")

    print("seed done: plot=%s report=%s" % (plot, rp))
    return True


def _create_complete_asset(device_id, kind, filename, flight_id,
                           capture_time, upload_time, data, duration_ms=None):
    conn = db()
    aid = services.new_id("as")
    conn.execute("""INSERT INTO media_assets
        (id,kind,filename,content_type,size_bytes,sha256,flight_id,device_id,
         capture_time,upload_time,duration_ms,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?, 'complete', ?)""",
        (aid, kind, filename, None, len(data), storage.put_bytes(data), flight_id,
         device_id, capture_time, upload_time, duration_ms, capture_time))
    conn.commit()
    return aid


def _placeholder_asset(device_id, kind, filename, flight_id, capture_time):
    conn = db()
    aid = services.new_id("as")
    conn.execute("""INSERT INTO media_assets
        (id,kind,filename,flight_id,device_id,capture_time,
         status,created_at) VALUES (?,?,?,?,?,?, 'pending_placeholder', ?)""",
        (aid, kind, filename, flight_id, device_id, capture_time, capture_time))
    conn.commit()
    return aid


if __name__ == "__main__":
    init()
    storage.init()
    seed_if_empty(force="--force" in sys.argv)
