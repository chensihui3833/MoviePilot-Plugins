"""SMB 硬链接清理的只读候选计算；不导入 MoviePilot，也不操作真实文件。"""

from pathlib import PurePosixPath

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg", ".iso", ".wmv"})


def normalize_root(value):
    """只接受 SMB 共享内部的非根绝对路径，不允许越界。"""
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("目录必须是 SMB 共享内的绝对路径")
    parts = value.split("/")
    if any(part in (".", "..") or ":" in part for part in parts):
        raise ValueError("目录不能包含路径穿越或协议地址")
    result = str(PurePosixPath(value))
    if result in ("/", ".") or result.startswith("//"):
        raise ValueError("不能监控共享根目录")
    return result


def validate_roots(source, library):
    source, library = normalize_root(source), normalize_root(library)
    a, b = source.casefold(), library.casefold()
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        raise ValueError("下载目录和媒体库目录不能相同或互相包含")
    return source, library


def parse_pairs(value):
    """每行一组下载目录和媒体库目录；任意两组目录也不能重叠。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请配置 SMB 目录对")
    pairs = []
    for line in value.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 2:
            raise ValueError("每行应为下载目录|媒体库目录")
        pairs.append(validate_roots(fields[0].strip(), fields[1].strip()))
    if not pairs or len(pairs) > 20:
        raise ValueError("目录对应为 1 到 20 组")
    roots = [root.casefold() for pair in pairs for root in pair]
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                raise ValueError("不同目录对不能相同或互相包含")
    return pairs


def identity(stat):
    """标识不可用即中止完整扫描，不能退化成 0 或空目录。"""
    dev, ino = getattr(stat, "st_dev", None), getattr(stat, "st_ino", None)
    size, mtime = getattr(stat, "st_size", None), getattr(stat, "st_mtime_ns", None)
    ctime = getattr(stat, "st_ctime_ns", None)
    if not isinstance(dev, int) or not isinstance(ino, int) or ino <= 0:
        raise ValueError("文件标识不可用")
    if not isinstance(size, int) or size < 0 or not isinstance(mtime, int) or mtime < 0 or not isinstance(ctime, int) or ctime < 0:
        raise ValueError("文件元数据不完整")
    return {"dev": dev, "ino": ino, "size": size, "mtime_ns": mtime, "ctime_ns": ctime}


def file_key(item):
    return item["dev"], item["ino"]


def calculate(previous, current):
    """比较两个完整快照，返回当前仍可复核的候选，不执行删除。"""
    if (not previous or previous.get("scope") != current.get("scope")
            or previous.get("session") != current.get("session")):
        return []
    old_library = previous["library"]
    new_library = current["library"]
    old_sources = previous["sources"]
    new_sources = current["sources"]
    live_library_keys = {file_key(item) for item in new_library.values()}
    old_sources_by_key = {}
    for path, item in old_sources.items():
        old_sources_by_key.setdefault(file_key(item), []).append((path, item))

    pending = {}
    for candidate in previous.get("candidates", []):
        source = candidate["source_key"]
        now = new_sources.get(source)
        if now == candidate["identity"] and file_key(now) not in live_library_keys:
            pending[(source, candidate["library_key"])] = {
                **candidate, "observations": candidate.get("observations", 1) + 1,
            }

    for library_path, old_item in old_library.items():
        old_key = file_key(old_item)
        now = new_library.get(library_path)
        if now is not None and file_key(now) == old_key:
            continue
        # 旧文件仍有媒体库路径，可能是重命名或多个媒体库链接。
        if old_key in live_library_keys:
            continue
        for source_path, source_item in old_sources_by_key.get(old_key, []):
            if new_sources.get(source_path) != source_item:
                continue
            pending[(source_path, library_path)] = {
                "source": source_item.get("path", source_path),
                "source_key": source_path,
                "library": old_item.get("path", library_path),
                "library_key": library_path,
                "reason": "替换" if now is not None else "删除",
                "identity": source_item,
                "first_seen": current.get("epoch", 0),
                "observations": 1,
            }
    return sorted(pending.values(), key=lambda item: (item["source"], item["library"]))
