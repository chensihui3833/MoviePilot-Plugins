"""Locate an actual BDMV disc/stream source, never infer episode from a clip number."""
from pathlib import Path
from .core import NeedsReview


def source_location(fileitem):
    if getattr(fileitem, "storage", None) not in (None, "local"):
        return None
    raw = Path(fileitem.path)
    kind = getattr(fileitem, "type", None)
    if kind == "dir" and (raw / "BDMV").is_dir():
        disc = raw
        video = None
    elif (kind == "file" and raw.suffix.lower() == ".m2ts"
          and raw.parent.name == "STREAM" and raw.parent.parent.name == "BDMV"):
        disc, video = raw.parents[2], raw
    else:
        return None
    if raw.is_symlink() or disc.is_symlink():
        raise NeedsReview("原盘或视频路径含符号链接")
    disc = disc.resolve(strict=True)
    return disc, video.resolve(strict=True) if video is not None else None
