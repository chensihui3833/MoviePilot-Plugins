"""Whole-file equivalence checks for in-mux Dolby Vision playlist subpaths."""
import struct
from .core import NeedsReview, playlist_items


def validate_subpaths(data):
    """Accept only a same-clip, synchronized DV enhancement layer (libbluray type10)."""
    clips = playlist_items(data)
    start = struct.unpack_from(">I", data, 8)[0]
    end = start + 4 + struct.unpack_from(">I", data, start)[0]
    count, subcount = struct.unpack_from(">HH", data, start + 6)
    cursor = start + 10
    primary = []
    for _ in range(count):
        size = struct.unpack_from(">H", data, cursor)[0]
        pos = cursor + 2
        primary.append(data[pos:pos + 20])
        cursor = pos + size
    if not subcount:
        return clips
    if count != 1 or subcount != 1:
        raise NeedsReview("播放列表含多个主片段或子路径，不能确认完整文件等价")
    main = primary[0]
    if cursor + 10 > end:
        raise NeedsReview("MPLS 子路径头不完整")
    length = struct.unpack_from(">I", data, cursor)[0]
    pos = cursor + 4
    subend = pos + length
    if length < 6 or subend > end:
        raise NeedsReview("MPLS 子路径长度错误")
    if data[pos + 1] != 10 or (struct.unpack_from(">H", data, pos + 2)[0] & 1) or data[pos + 5] != 1:
        raise NeedsReview("仅支持同文件同步的杜比视界增强层，其他子路径需要核对")
    cursor = pos + 6
    if cursor + 2 > subend:
        raise NeedsReview("MPLS 子视频条目不完整")
    size = struct.unpack_from(">H", data, cursor)[0]
    pos = cursor + 2
    if size != 28 or pos + size != subend:
        raise NeedsReview("MPLS 子视频长度或条目数量不受支持")
    item = data[pos:subend]
    flags = struct.unpack_from(">I", item, 9)[0]
    main_in, main_out = struct.unpack_from(">II", main, 12)
    sub_in, sub_out, sync_id, sync_pts = struct.unpack_from(">IIHI", item, 14)
    # Bit0 is multi-clip; bits1..4 are the connection condition.
    if (item[:9] != main[:9] or item[13] != main[11] or flags & 1
            or (flags >> 1) & 15 not in (1, 5, 6)
            or sync_id != 0 or sync_pts != main_in
            or sub_in != main_in or abs(sub_out - main_out) > 1):
        raise NeedsReview("杜比视界子路径引用其他文件、时间范围或同步关系，不能硬链接代替")
    if subend != end:
        raise NeedsReview("MPLS 子路径包含未核对的额外内容")
    return clips
