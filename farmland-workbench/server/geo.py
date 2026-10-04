# -*- coding: utf-8 -*-
"""
地理配准与地块归属。

铁律: 镜头画面坐标 (像素) 不能直接当作地理坐标。
像素 -> 经纬度的合法通道:
  1) exif_gps         设备 GNSS 标签 (误差由设备精度给出)
  2) manual_pin       人工在地图上刺点 (误差由操作者估计)
  3) ortho_match      通过已配准影像控制点做仿射变换 (误差由控制点 RMSE 推出)
  4) frame_projection 相机姿态 + DEM/平面假设的投影 (误差由姿态/地形不确定性推出)
任何来源都必须同时给 geo_error_m。

归属判断:
  点在多边形内            -> auto_inside
  点不在内部但到边界距离 <= geo_error_m -> auto_buffer (边界不确定, 需人工确认)
  否则                    -> none
"""
import json
import math

R_EARTH = 6378137.0
VALID_METHODS = {"exif_gps", "manual_pin", "ortho_match", "frame_projection"}


def meters_per_degree(lat):
    lat_r = math.radians(lat)
    return (R_EARTH * math.radians(1),
            R_EARTH * math.radians(1) * math.cos(lat_r))  # (m/lat, m/lon)


def local_offset(lon, lat, lon0, lat0):
    """返回相对参考点的平面偏移 (east_m, north_m)。"""
    mlat, mlon = meters_per_degree(lat0)
    return ((lon - lon0) * mlon, (lat - lat0) * mlat)


def distance_m(lon1, lat1, lon2, lat2):
    # 等距圆柱近似 (农田尺度足够)
    mlat, mlon = meters_per_degree((lat1 + lat2) / 2)
    dx = (lon2 - lon1) * mlon
    dy = (lat2 - lat1) * mlat
    return math.hypot(dx, dy)


def point_in_ring(x, y, ring):
    """射线法; ring 为 [[lon,lat], ...]。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-18) + xi):
            inside = not inside
        j = i
    return inside


def point_in_polygon(lon, lat, polygon):
    """polygon: GeoJSON Polygon coordinates (外环 + 洞)。"""
    rings = polygon["coordinates"] if isinstance(polygon, dict) else polygon
    if not rings:
        return False
    if not point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def point_to_segment_distance_m(px, py, ax, ay, bx, by, lat0):
    """平面米制下点到线段距离。"""
    mlat, mlon = meters_per_degree(lat0)
    Px, Py = px * mlon, py * mlat
    Ax, Ay = ax * mlon, ay * mlat
    Bx, By = bx * mlon, by * mlat
    dx, dy = Bx - Ax, By - Ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(Px - Ax, Py - Ay)
    t = max(0.0, min(1.0, ((Px - Ax) * dx + (Py - Ay) * dy) / L2))
    qx, qy = Ax + t * dx, Ay + t * dy
    return math.hypot(Px - qx, Py - qy)


def distance_to_polygon_edge_m(lon, lat, polygon):
    rings = polygon["coordinates"] if isinstance(polygon, dict) else polygon
    best = float("inf")
    for ring in rings:
        for i in range(len(ring) - 1):
            d = point_to_segment_distance_m(lon, lat, ring[i][0], ring[i][1],
                                            ring[i + 1][0], ring[i + 1][1], lat)
            best = min(best, d)
    return best


def attribute(lon, lat, polygon_geojson):
    """返回 (status, distance_to_edge_m)。"""
    poly = json.loads(polygon_geojson) if isinstance(polygon_geojson, str) else polygon_geojson
    if point_in_polygon(lon, lat, poly):
        return "auto_inside", distance_to_polygon_edge_m(lon, lat, poly)
    return "none", distance_to_polygon_edge_m(lon, lat, poly)


def attribute_with_error(lon, lat, polygon_geojson, geo_error_m):
    """带误差环的归属: 内部 -> auto_inside; 边界在误差内 -> auto_buffer; 否则 none。"""
    status, edge = attribute(lon, lat, polygon_geojson)
    if status == "auto_inside":
        return "auto_inside", edge
    if edge <= max(geo_error_m or 0.0, 0.0):
        return "auto_buffer", edge
    return "none", edge


# --------------------------------------------------------------------------
# 镜头画面坐标 -> 地理坐标: frame_projection
# 相机模型 (简化但显式):
#   - 无人机位置 (drone_lon, drone_lat, drone_alt_m) 带 GNSS 误差
#   - 航向 yaw_deg (北=0, 顺时针), 俯仰 pitch_deg (水平=0, 向下为负), 滚转 roll_deg
#   - 镜头焦距像素 focal_px, 画面宽高 frame_w/frame_h
#   - 像主点 (cx, cy), 目标像素 (px, py)
#   - 地面假设平面 (h=0), 与起飞点同高
# 输出 (lon, lat, error_m)。误差来源显式传播:
#   GNSS 水平误差 + 姿态角误差 + 焦距/量测误差 + 地形高度误差。
# --------------------------------------------------------------------------
def frame_projection(drone_lon, drone_lat, drone_alt_m,
                     yaw_deg, pitch_deg, roll_deg,
                     focal_px, frame_w, frame_h,
                     px, py,
                     cx=None, cy=None,
                     gnss_error_m=3.0, attitude_error_deg=2.0,
                     terrain_height_uncertainty_m=5.0):
    if frame_w <= 0 or frame_h <= 0 or focal_px <= 0:
        raise ValueError("focal_px / frame_w / frame_h 必须为正")
    if drone_alt_m <= 0:
        raise ValueError("无人机对地高度必须为正")
    if not (0 <= px <= frame_w and 0 <= py <= frame_h):
        raise ValueError("像素坐标超出画面范围")
    if cx is None:
        cx = frame_w / 2.0
    if cy is None:
        cy = frame_h / 2.0

    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)

    # 相机坐标系: x 右, y 下, z 前(视线方向)
    rx = px - cx
    ry = py - cy
    # 归一化视线向量
    v = __normalize((rx, ry, focal_px))

    # 相机自身零姿态 (pitch=roll=yaw=0) 时: z前=北, x右=东, y下=地
    # 先施加滚转(绕前 z 轴), 再俯仰(绕右 x 轴, 向下为负)
    v = __rot_axis(v, roll, (0, 0, 1))     # 绕前轴滚转
    v = __rot_axis(v, pitch, (1, 0, 0))    # 绕右轴俯仰
    # 相机轴 -> 世界 (东, 北, 地): x右->东, z前->北, y下->地
    ex, down, nf = v
    # 航向 yaw: 水平面绕地轴顺时针 (北=0)
    ca, sa = math.cos(yaw), math.sin(yaw)
    east = ca * ex + sa * nf
    north = -sa * ex + ca * nf
    v_world = (east, north, down)

    if v_world[2] <= 1e-9:
        raise ValueError("该像素的视线不与地面相交 (指向地平线以上)")

    # 平面地面 h=0: 从高度 H 沿视线到地
    t = drone_alt_m / v_world[2]
    east_m, north_m = t * v_world[0], t * v_world[1]

    mlat, mlon = meters_per_degree(drone_lat)
    lon = drone_lon + east_m / mlon
    lat = drone_lat + north_m / mlat

    # --- 误差传播 (显式、保守一阶) ---
    # 1) 视线倾角误差: 姿态误差使地面距离产生误差 ~ t * d_angle
    d_att = t * math.radians(attitude_error_deg)
    # 2) 像素量测/焦距误差: 等效 1px 视角
    d_px = t * (1.0 / focal_px)
    # 3) 地形高度不确定: 倾角越大影响越大
    slant = math.hypot(1.0, v_world[2])
    d_terrain = terrain_height_uncertainty_m * (1.0 / max(v_world[2], 1e-6))
    # 4) GNSS 直接平移
    error_m = math.sqrt(gnss_error_m ** 2 + d_att ** 2 + d_px ** 2 + d_terrain ** 2)
    return lon, lat, round(error_m, 2)


def __normalize(v):
    n = math.sqrt(sum(c * c for c in v))
    return (v[0] / n, v[1] / n, v[2] / n)


def __rot_axis(v, angle, axis):
    # Rodrigues
    a = __normalize(axis)
    c, s = math.cos(angle), math.sin(angle)
    dot = sum(a[i] * v[i] for i in range(3))
    cross = (a[1] * v[2] - a[2] * v[1],
             a[2] * v[0] - a[0] * v[2],
             a[0] * v[1] - a[1] * v[0])
    return tuple(v[i] * c + cross[i] * s + a[i] * dot * (1 - c) for i in range(3))


def validate_geo(method, lon, lat, error_m):
    """所有地理落库前统一校验: 禁止"裸"坐标。"""
    if method is None and lon is None and lat is None:
        return  # 纯像素证据允许无地理坐标
    if method not in VALID_METHODS:
        raise ValueError("geo_method 非法或缺失 (像素坐标不能直接当地理坐标)")
    if lon is None or lat is None or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        raise ValueError("地理坐标非法")
    if error_m is None or error_m < 0:
        raise ValueError("必须给出定位误差 geo_error_m")
