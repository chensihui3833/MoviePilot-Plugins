"""Read Sony/onQ episodic BDMV metadata and create additive hardlinks/NFO."""

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


class NeedsReview(ValueError):
    pass


def bounded_read(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise NeedsReview(f"文件超过解析大小限制：{path}")
    return data


def playlist_items(data):
    if len(data) < 20 or data[:4] != b"MPLS":
        raise NeedsReview("不是有效的 MPLS 播放列表")
    start = struct.unpack_from(">I", data, 8)[0]
    if start + 10 > len(data):
        raise NeedsReview("MPLS 播放列表偏移越界")
    length = struct.unpack_from(">I", data, start)[0]
    end = start + 4 + length
    if end > len(data):
        raise NeedsReview("MPLS 播放列表不完整")
    count = struct.unpack_from(">H", data, start + 6)[0]
    cursor = start + 10
    items = []
    for _ in range(count):
        if cursor + 22 > end:
            raise NeedsReview("MPLS 视频条目不完整")
        size = struct.unpack_from(">H", data, cursor)[0]
        payload = cursor + 2
        if size < 20 or payload + size > end:
            raise NeedsReview("MPLS 视频条目长度错误")
        clip = data[payload:payload + 5].decode("ascii", errors="replace")
        if not re.fullmatch(r"\d{5}", clip) or data[payload + 5:payload + 9] not in (b"M2TS", b"FMTS"):
            raise NeedsReview("MPLS 视频编号或格式不受支持")
        flags = struct.unpack_from(">H", data, payload + 9)[0]
        if flags & 0x10:
            raise NeedsReview("多角度播放列表需要人工核对")
        time_in, time_out = struct.unpack_from(">II", data, payload + 12)
        if time_out <= time_in:
            raise NeedsReview("MPLS 视频时间范围错误")
        items.append({"clip": clip, "in": time_in / 45000, "out": time_out / 45000})
        cursor = payload + size
    return items


def read_titles(jar):
    titles = {}
    with zipfile.ZipFile(jar) as archive:
        for info in archive.infolist():
            match = re.fullmatch(r"eng/tblEP_T3_(\d{2,3})_res\.qct", info.filename)
            if not match:
                continue
            if info.file_size > 65536 or info.compress_size > 65536:
                raise NeedsReview("菜单标题资源大小异常")
            data = archive.read(info)
            if not data.startswith(b"QCTF"):
                raise NeedsReview("菜单标题资源格式不受支持")
            matches = re.findall(rb"(?:^|\x00)TITLE\x00([^\x00]+)\x00Menu Title\x00", data)
            if len(matches) != 1:
                raise NeedsReview("菜单资源没有唯一、明确的标题")
            title = " ".join(matches[0].decode("utf-8").split())
            number = int(match[1])
            if not title or number in titles:
                raise NeedsReview("标题为空或菜单集号重复")
            titles[number] = title
    if not titles or sorted(titles) != list(range(1, len(titles) + 1)):
        raise NeedsReview("没有连续、完整的英文菜单标题表")
    return titles


def read_episode_playlists(xml_path):
    data = bounded_read(xml_path, 2 * 1024 * 1024)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise NeedsReview("拒绝带实体声明的菜单 XML")
    root = ET.fromstring(data)
    episodes = {}
    for entry in root.findall("playlist"):
        match = re.fullmatch(r"EP\s+(\d+)", entry.get("name", ""), re.IGNORECASE)
        if not match:
            continue
        number = int(match[1])
        pid = entry.get("id", "")
        if number in episodes or not re.fullmatch(r"\d{5}", pid):
            raise NeedsReview("正片集号重复或播放列表编号无效")
        episodes[number] = (pid, int(entry.get("duration_ms", "0")) / 1000)
    if not episodes or sorted(episodes) != list(range(1, len(episodes) + 1)):
        raise NeedsReview("没有连续、明确的盘内 EP 集号")
    return episodes


def probe_video(path, executable):
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=start_time,duration",
             "-of", "json", str(path)],
            capture_output=True, timeout=45, check=True,
        )
        info = json.loads(result.stdout)["format"]
        return float(info["start_time"]), float(info["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        raise NeedsReview(f"无法核对视频起止时间：{path.name}") from exc


def inside(path, root):
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise NeedsReview(f"源文件超出所选目录：{path}")
    return resolved


def scan_season(source_dir, series, season, output_root, tmdb_id="", ffprobe=None):
    if not all(str(value).strip() for value in (source_dir, output_root)):
        raise NeedsReview("源目录与分集目录均须填写")
    source = Path(source_dir).resolve(strict=True)
    output = Path(output_root).resolve()
    if not source.is_dir():
        raise NeedsReview("整季源目录不存在")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise NeedsReview("索引输出目录与原盘源目录必须分开，不能互相包含")
    if not series.strip() or any(c in series for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in series) or series.strip() in (".", ".."):
        raise NeedsReview("剧名不能为空，也不能包含路径特殊字符")
    season = int(season)
    if not 1 <= season <= 999:
        raise NeedsReview("首版支持第 1–999 季；特别篇需要人工核对")
    if tmdb_id and not str(tmdb_id).isdigit():
        raise NeedsReview("TMDB 编号必须为数字")
    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        raise NeedsReview("未找到 ffprobe；为避免错误分集，本次不生成播放入口")
    discs = {}
    for entry in source.iterdir():
        if entry.is_dir():
            match = re.fullmatch(r"(?:disc|disk)[ ._-]*0*(\d+)", entry.name, re.IGNORECASE)
            if match:
                number = int(match[1])
                if number in discs:
                    raise NeedsReview("同一盘号出现多个目录")
                discs[number] = inside(entry, source)
    if not discs or sorted(discs) != list(range(1, len(discs) + 1)):
        raise NeedsReview("请选择完整一季：盘号必须从 DISC1 开始连续，不能缺盘")
    all_titles = None
    rows = []
    evidence = []
    for disc_number, disc in sorted(discs.items()):
        jar = inside(disc / "BDMV/JAR/00002.jar", source)
        xml = inside(disc / "BDMV/JAR/00002/playlists.xml", source)
        if jar.stat().st_size > 32 * 1024 * 1024:
            raise NeedsReview("菜单 JAR 超过首版解析大小限制")
        titles = read_titles(jar)
        if all_titles is None:
            all_titles = titles
        elif titles != all_titles:
            raise NeedsReview("各盘没有一致的整季标题表，不能按整季顺序自动对应")
        for path in (jar, xml):
            evidence.append(hashlib.sha256(bounded_read(path, 32 * 1024 * 1024)).hexdigest())
        for local_episode, (pid, xml_duration) in sorted(read_episode_playlists(xml).items()):
            mpls = inside(disc / "BDMV/PLAYLIST" / (pid + ".mpls"), source)
            blob = bounded_read(mpls, 8 * 1024 * 1024)
            evidence.append(hashlib.sha256(blob).hexdigest())
            clips = playlist_items(blob)
            if len(clips) != 1:
                raise NeedsReview(f"第 {disc_number} 盘 EP {local_episode} 由多个片段组成，首版暂不导出")
            clip = clips[0]
            duration = clip["out"] - clip["in"]
            if abs(duration - xml_duration) > 1:
                raise NeedsReview("菜单时长与实际播放列表不一致")
            video = inside(disc / "BDMV/STREAM" / (clip["clip"] + ".m2ts"), source)
            start, video_duration = probe_video(video, executable)
            if not all(math.isfinite(v) for v in (start, video_duration)) or abs(start - clip["in"]) > 0.5 or abs(start + video_duration - clip["out"]) > 0.5:
                raise NeedsReview(f"{pid}.mpls 只播放视频的一部分，不能直接生成整文件入口")
            stat = video.stat()
            if "\n" in str(video) or "\r" in str(video):
                raise NeedsReview("视频路径含换行符")
            rows.append({
                "episode": len(rows) + 1, "disc": disc_number, "disc_episode": local_episode,
                "playlist": pid, "clip": clip["clip"], "seconds": round(duration, 3),
                "video": str(video), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino, "device": stat.st_dev,
            })
    if len(rows) != len(all_titles):
        raise NeedsReview("各盘正片总数与整季菜单标题总数不一致，可能缺盘或有特别篇")
    if len({r["video"] for r in rows}) != len(rows):
        raise NeedsReview("多个集号指向同一视频，需要人工核对")
    for row in rows:
        row["title"] = all_titles[row["episode"]]
    plan = {
        "format": "sony-onq-complete-season-hardlink-v1", "source": str(source),
        "series": series.strip(), "season": season, "tmdb_id": str(tmdb_id),
        "output": str(output / series.strip()), "episodes": rows,
        "evidence": evidence,
        "mapping_basis": "完整连续盘序 + 每盘 EP 顺序 + 所有盘一致的整季标题表；导出前请核对预览",
    }
    plan["fingerprint"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return plan


def xml_bytes(root):
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def render_files(plan):
    files = {}
    show = ET.Element("tvshow")
    ET.SubElement(show, "title").text = plan["series"]
    if plan["tmdb_id"]:
        ET.SubElement(show, "uniqueid", {"type": "tmdb", "default": "true"}).text = plan["tmdb_id"]
    if plan.get("write_tvshow", True):
        files["tvshow.nfo"] = xml_bytes(show)
    for row in plan["episodes"]:
        relative = row.get("relative") or f"Season {plan['season']}/{plan['series']} - S{plan['season']:02}E{row['episode']:02}.m2ts"
        stem = str(Path(relative).with_suffix(""))
        episode = ET.Element("episodedetails")
        for key, value in (("title", row["title"]), ("showtitle", plan["series"]),
                           ("season", plan["season"]), ("episode", row["episode"]),
                           ("runtime", round(row["seconds"] / 60))):
            ET.SubElement(episode, key).text = str(value)
        files[stem + ".nfo"] = xml_bytes(episode)
    return files


def render_links(plan):
    return {
        row.get("relative") or f"Season {plan['season']}/{plan['series']} - S{plan['season']:02}E{row['episode']:02}.m2ts": row
        for row in plan["episodes"]
    }


def matching_nfo(path, content, preserve_enriched=False):
    if not path.is_file():
        return False
    existing = bounded_read(path, 2 * 1024 * 1024)
    if existing == content:
        return True
    if not preserve_enriched or b"<!DOCTYPE" in existing.upper() or b"<!ENTITY" in existing.upper():
        return False
    try:
        old, new = ET.fromstring(existing), ET.fromstring(content)
        return (old.tag == new.tag == "episodedetails"
                and all(old.findtext(k) == new.findtext(k) for k in ("season", "episode")))
    except ET.ParseError:
        return False


def export_plan(plan, approved_fingerprint, preview=False, should_stop=None):
    if not approved_fingerprint or approved_fingerprint != plan["fingerprint"]:
        raise NeedsReview("请先预览；源内容或配置已变化时必须重新预览后再导出")
    root = Path(plan["output"])
    source = Path(plan["source"]).resolve(strict=True)
    root_resolved = root.resolve()
    if root_resolved.is_relative_to(source) or source.is_relative_to(root_resolved):
        raise NeedsReview("输出目录与原盘目录重叠")
    files = render_files(plan)
    links = render_links(plan)
    if len(links) != len(plan["episodes"]):
        raise NeedsReview("多个集号对应同一目标文件，命名规则必须包含集号")
    ancestor = root_resolved
    while not ancestor.exists():
        ancestor = ancestor.parent
    output_device = ancestor.stat().st_dev
    for relative, row in links.items():
        video = inside(row["video"], source)
        stat = video.stat()
        if any(getattr(stat, attr) != row[key] for attr, key in (
            ("st_size", "size"), ("st_mtime_ns", "mtime_ns"),
            ("st_ino", "inode"), ("st_dev", "device"),
        )):
            raise NeedsReview("源视频在预览后发生变化，请重新预览")
        if stat.st_dev != output_device:
            raise NeedsReview("源视频和输出目录不在同一文件系统，不能创建硬链接")
        path = root / relative
        if not path.resolve().is_relative_to(root_resolved) or path.is_symlink():
            raise NeedsReview("输出路径包含异常跳转")
        if path.exists() and not path.samefile(video):
            raise NeedsReview(f"已有不同视频，保留原文件并停止：{path}")
    # Preflight the whole batch before writing; never replace or remove an existing file.
    for relative, content in files.items():
        path = root / relative
        if not path.resolve().is_relative_to(root_resolved) or path.is_symlink():
            raise NeedsReview("输出路径包含异常跳转")
        if path.exists() and not matching_nfo(path, content, plan.get("preserve_enriched_nfo", False)):
            raise NeedsReview(f"已有不同内容，保留原文件并停止：{path}")
    if preview:
        return {"episodes": len(plan["episodes"]), "created_files": 0, "output": str(root)}
    written = 0
    for relative, row in links.items():
        if should_stop and should_stop():
            raise NeedsReview("整理已停止；已生成入口保留，源文件未删除")
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(row["video"], path)
            written += 1
        except FileExistsError:
            if not path.samefile(row["video"]):
                raise NeedsReview(f"输出期间出现视频冲突，未覆盖：{path}")
        except OSError as exc:
            raise NeedsReview(f"硬链接创建失败；已创建的入口保留，可修复后重试：{exc}") from exc
    for relative, content in files.items():
        if should_stop and should_stop():
            raise NeedsReview("整理已停止；已生成入口保留，源文件未删除")
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(content)
            written += 1
        except FileExistsError:
            if not matching_nfo(path, content, plan.get("preserve_enriched_nfo", False)):
                raise NeedsReview(f"输出期间出现文件冲突，未覆盖：{path}")
    return {"episodes": len(plan["episodes"]), "created_files": written, "output": str(root)}
