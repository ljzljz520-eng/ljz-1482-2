# -*- coding: utf-8 -*-
"""
对象存储 (本地文件模拟 S3, 内容寻址)。
key = sha256, objects/<前2位>/<sha256>。同内容重复上传 -> 同对象 (秒传/块去重)。
"""
import hashlib
import os
import shutil

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "storage")


def _path(key):
    if not key or "/" in key or ".." in key or len(key) < 3:
        raise ValueError("非法 object key")
    return os.path.join(ROOT, "objects", key[:2], key)


def init():
    os.makedirs(os.path.join(ROOT, "objects"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "derived"), exist_ok=True)


def exists(key):
    return key is not None and os.path.isfile(_path(key))


def put_bytes(data, key=None):
    digest = key or hashlib.sha256(data).hexdigest()
    p = _path(digest)
    if not os.path.isfile(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    return digest


def put_file(tmp_path, key):
    p = _path(key)
    if os.path.isfile(p):
        os.remove(tmp_path)
        return key
    os.makedirs(os.path.dirname(p), exist_ok=True)
    os.replace(tmp_path, p)
    return key


def put_stream_iter(chunks, expect_sha=None):
    """流式写入并实时校验 sha256; 返回 sha256。"""
    os.makedirs(os.path.join(ROOT, "objects"), exist_ok=True)
    tmp = os.path.join(ROOT, "derived", "tmp-%s" % (expect_sha or os.getpid()))
    h = hashlib.sha256()
    with open(tmp, "wb") as f:
        for c in chunks:
            h.update(c)
            f.write(c)
    digest = h.hexdigest()
    if expect_sha and digest != expect_sha:
        os.remove(tmp)
        raise ValueError("整体 sha256 校验失败: 期望 %s, 实际 %s" % (expect_sha, digest))
    return put_file(tmp, digest)


def get_path(key):
    p = _path(key)
    if not os.path.isfile(p):
        raise FileNotFoundError(key)
    return p


def open_ro(key):
    return open(get_path(key), "rb")


def size(key):
    return os.path.getsize(get_path(key))


def put_derived(name, data):
    """派生文件 (裁切视频/导出zip/预览包) 按名存放。"""
    p = os.path.join(ROOT, "derived", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def derived_path(name):
    return os.path.join(ROOT, "derived", name)
