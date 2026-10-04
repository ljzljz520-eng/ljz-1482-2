"""Geo helpers: ray-casting point-in-polygon, flat-earth projection,
and strict camera-frame -> ground projection (镜头画面坐标绝不能直接当地理坐标).

Camera convention
-----------------
* frame 归一化坐标 (cx, cy) ∈ [0,1]^2, 原点左上; (0.5,0.5) 为画面中心。
* gimbal pitch: -90 = 正下方 (nadir), 0 = 水平向前。
* yaw: 0 = 正北, 顺时针为正。
* 交点需无人机相对地面的高度 H = drone_alt - ground_elevation。
"""
import json, math

R_EARTH = 6378137.0

def meters_per_deg(lat):
    return 111320.0 * math.cos(math.radians(lat)), 110540.0

def project_ray(lat0, lng0, drone_alt, ground_elev, yaw, pitch,
                cx, cy, fov_h, fov_v):
    """Project one camera-frame pixel ray onto the ground via vector geometry.

    Optical axis d0 from gimbal yaw/pitch; ray = normalize(d0 + R*tan a + U*tan b),
    then intersect with the ground plane (z = ground_elev).
    """
    H = drone_alt - ground_elev
    if H <= 0:
        raise ValueError("drone altitude must be above ground elevation")
    psi = math.radians(yaw)
    p0 = math.radians(pitch)
    # optical axis in (E, N, U)
    d0 = (math.cos(p0) * math.sin(psi),
          math.cos(p0) * math.cos(psi),
          math.sin(p0))
    right = (math.cos(psi), -math.sin(psi), 0.0)
    upf = (right[1]*d0[2] - right[2]*d0[1],
           right[2]*d0[0] - right[0]*d0[2],
           right[0]*d0[1] - right[1]*d0[0])  # cross(right, d0)
    a = math.radians((cx - 0.5) * fov_h)
    b = math.radians((0.5 - cy) * fov_v)
    ray = [d0[i] + right[i]*math.tan(a) + upf[i]*math.tan(b) for i in range(3)]
    norm = math.sqrt(sum(v*v for v in ray))
    de0, dn0, du0 = (ray[0]/norm, ray[1]/norm, ray[2]/norm)
    if du0 >= 0:
        raise ValueError("ray does not intersect ground (points above horizon)")
    t = H / -du0
    e, n = t * de0, t * dn0
    mplng, mplat = meters_per_deg(lat0)
    bearing = (math.degrees(math.atan2(de0, dn0))) % 360.0
    rng = math.hypot(e, n)
    offnadir = math.degrees(math.acos(min(1.0, -du0)))
    return lat0 + n / mplat, lng0 + e / mplng, bearing, offnadir, rng, H

def footprint_polygon(pos):
    """Project the four frame corners -> GeoJSON polygon (lon,lat)."""
    corners = [(0,0),(1,0),(1,1),(0,1),(0,0)]
    ring = []
    for cx, cy in corners:
        lat, lng, *_ = project_ray(
            pos["drone_lat"], pos["drone_lng"], pos["drone_alt"],
            pos.get("ground_elevation", 0.0), pos.get("yaw", 0.0),
            pos.get("pitch", -90.0), cx, cy,
            pos.get("fov_h", 70.0), pos.get("fov_v", 50.0))
        ring.append([round(lng, 8), round(lat, 8)])
    return {"type": "Polygon", "coordinates": [ring]}

def georeference(pos, frame_width_px=None):
    """Compute ground center, footprint and horizontal error estimate.

    Error budget (1-sigma, quadrature):
      * drone_hrms                 水平定位误差
      * sigma_v * tan(theta)       高程/DEM 误差经斜视放大
      * 2 像素指向误差在地面的尺度   H/cos^2(theta) * 像素张角
      * 3 m 常值底噪
    """
    fov_h = pos.get("fov_h", 70.0); fov_v = pos.get("fov_v", 50.0)
    lat, lng, bearing, offnadir, rng, H = project_ray(
        pos["drone_lat"], pos["drone_lng"], pos["drone_alt"],
        pos.get("ground_elevation", 0.0), pos.get("yaw", 0.0),
        pos.get("pitch", -90.0), pos.get("frame_cx", 0.5),
        pos.get("frame_cy", 0.5), fov_h, fov_v)
    fp = footprint_polygon(pos)
    hrms = pos.get("drone_hrms") or 5.0
    # DEM error: SRTM ~5 m, 本地 DSM ~1 m
    dem_err = {"dsm_local": 1.0, "srtm": 5.0}.get(pos.get("dem_source"), 5.0)
    sigma_v = math.sqrt(max(hrms, 1.0) ** 2 + dem_err ** 2)
    theta = math.radians(offnadir)
    e_dem = sigma_v * math.tan(theta)
    px_angle = math.radians(fov_h / max(pos.get("frame_width_px") or 4000, 1))
    e_pt = 2.0 * H * px_angle / (math.cos(theta) ** 2)
    radius = math.sqrt(hrms ** 2 + e_dem ** 2 + e_pt ** 2 + 3.0 ** 2)
    return {
        "center_lng": round(lng, 8), "center_lat": round(lat, 8),
        "radius_m": round(radius, 2), "footprint_geojson": fp,
        "horizontal_error_m": round(radius, 2),
        "geo_method": "pinhole_flatearth_dem:%s" % (pos.get("dem_source") or "none"),
        "offnadir_deg": round(offnadir, 2), "range_m": round(rng, 2),
    }

# ---------- planar geometry ----------
def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def point_in_polygon(lng, lat, geojson):
    if not geojson:
        return False
    g = geojson if isinstance(geojson, dict) else json.loads(geojson)
    if g.get("type") == "Polygon":
        rings = g["coordinates"]
        if not point_in_ring(lng, lat, rings[0]):
            return False
        for hole in rings[1:]:
            if point_in_ring(lng, lat, hole):
                return False
        return True
    if g.get("type") == "MultiPolygon":
        return any(point_in_polygon(lng, lat, {"type": "Polygon", "coordinates": poly})
                   for poly in g["coordinates"])
    return False

def haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R_EARTH * math.asin(math.sqrt(a))

def polygon_centroid(geojson):
    g = geojson if isinstance(geojson, dict) else json.loads(geojson)
    ring = g["coordinates"][0] if g.get("type") == "Polygon" else g["coordinates"][0][0]
    x = sum(p[0] for p in ring) / len(ring)
    y = sum(p[1] for p in ring) / len(ring)
    return x, y

def circle_polygon(lng, lat, radius_m, n=24):
    mplng, mplat = meters_per_deg(lat)
    ring = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        ring.append([round(lng + radius_m * math.cos(a) / mplng, 8),
                     round(lat + radius_m * math.sin(a) / mplat, 8)])
    return {"type": "Polygon", "coordinates": [ring]}
