"""QB 任务文件路径与清理候选的纯计算；此模块不执行下载器操作。"""

import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from .core import VIDEO_EXTENSIONS

_HASH = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")


def _parts(value, *, absolute):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("文件路径无效")
    if value.startswith("/") != absolute or value.startswith("//"):
        raise ValueError("文件路径类型不匹配")
    parts = value.split("/")
    if any(part in (".", "..") or ":" in part for part in parts):
        raise ValueError("文件路径越界")
    return [part for part in parts if part]


def remote_file(save_path, torrent_name):
    """只接受 MP 已反向映射的本地保存目录和 QB 相对文件名。"""
    if not isinstance(save_path, str) or not save_path.startswith("/"):
        raise ValueError("下载器保存路径未映射为本地路径")
    root = _parts(save_path, absolute=True)
    relative = _parts(torrent_name, absolute=False)
    if not relative:
        raise ValueError("下载器文件名为空")
    return "/" + "/".join(root + relative)


def source_path(value):
    parts = _parts(value, absolute=True)
    if not parts:
        raise ValueError("源文件不能是共享根目录")
    return "/" + "/".join(parts)


def inventory(torrent_hash, save_path, files):
    """仅从指定 QB 任务真实文件清单构造视频路径，不猜测种子目录。"""
    if not isinstance(torrent_hash, str) or not _HASH.fullmatch(torrent_hash):
        raise ValueError("种子哈希无效")
    if not isinstance(files, list) or not files:
        raise ValueError("种子文件清单为空")
    paths = []
    seen = set()
    for entry in files:
        name = entry.get("name") if isinstance(entry, Mapping) else getattr(entry, "name", None)
        path = remote_file(save_path, name)
        if path in seen:
            raise ValueError("种子文件路径重复")
        seen.add(path)
        if PurePosixPath(path).suffix.casefold() in VIDEO_EXTENSIONS:
            paths.append(path)
    if not paths:
        raise ValueError("种子内没有可识别的视频")
    return {"hash": torrent_hash.lower(), "files": sorted(paths)}


def task_decision(task, candidate_paths, retired_paths=()):
    """清理前先暂停；只有每个视频都已确认清理才允许删任务。"""
    videos = task.get("files")
    if not isinstance(videos, list) or not videos or len(videos) != len(set(videos)):
        raise ValueError("视频清单不完整")
    candidates = {source_path(path) for path in candidate_paths}
    retired = {source_path(path) for path in retired_paths}
    video_set = {source_path(path) for path in videos}
    matching = sorted(video_set & candidates)
    return {"matches": matching, "must_pause": bool(matching),
            "may_remove": bool(video_set) and video_set <= retired,
            "remaining": sorted(video_set - retired)}
