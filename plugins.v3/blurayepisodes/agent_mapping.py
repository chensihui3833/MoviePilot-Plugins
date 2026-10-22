"""Validate assistant decisions using disc files; retain the 0.4.0 export contract."""
import hashlib
import json
import math
import re
import shutil
import time
from pathlib import Path

from .playlist_guard import validate_subpaths
from .disc_layout import disc_number
from .core import NeedsReview, bounded_read, inside, probe_video


def discs_in(source):
    source = Path(source).resolve(strict=True)
    discs = {}
    for path in source.iterdir():
        if path.is_dir() and (path / "BDMV").is_dir():
            number = disc_number(path)
            if number is None:
                raise NeedsReview("无法确定某个原盘的盘号，请核对目录或盘内身份信息")
            if path.is_symlink() or number in discs:
                raise NeedsReview("盘目录存在跳转或重复盘号")
            discs[number] = inside(path, source)
    if not discs or sorted(discs) != list(range(1, len(discs) + 1)):
        raise NeedsReview("请选择盘号从1开始连续的完整单季 BDMV 目录")
    return discs


def snapshot(source_dir):
    """No video bytes are read. Metadata hashes and all file identities invalidate reuse."""
    source = Path(source_dir).resolve(strict=True)
    if not source.is_dir():
        raise NeedsReview("整季源目录不存在")
    discs = discs_in(source)
    digest = hashlib.sha256()
    count = total_metadata = 0
    for number, disc in sorted(discs.items()):
        digest.update(f"disc:{number}:{disc.name}\n".encode())
        for path in sorted(disc.rglob("*")):
            count += 1
            if count > 50000 or path.is_symlink():
                raise NeedsReview("源目录文件过多或含符号链接，停止自动分析")
            if not path.is_file():
                continue
            stat = path.stat()
            fields = [str(path.relative_to(source)), stat.st_size, stat.st_mtime_ns,
                      stat.st_dev, stat.st_ino]
            if path.suffix.lower() not in {".m2ts", ".ssif", ".mkv", ".mp4", ".iso"}:
                total_metadata += stat.st_size
                if total_metadata > 256 * 1024 * 1024:
                    raise NeedsReview("盘内元数据超过本次分析大小限制")
                fields.append(hashlib.sha256(bounded_read(path, 32 * 1024 * 1024)).hexdigest())
            digest.update(json.dumps(fields, ensure_ascii=False).encode())
    return digest.hexdigest()


def relative_file(value, source):
    if not isinstance(value, str) or not value or "\\" in value:
        raise NeedsReview("助手提交了无效源路径")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise NeedsReview("助手提交的源路径必须是季目录内的相对路径")
    result = inside(source / path, source)
    if not result.is_file():
        raise NeedsReview("助手提交的证据或播放列表不存在")
    return result


def checked_plan(source_dir, series, season, output_root, mapping, tmdb_id="", ffprobe=None, expected_episodes=None):
    source = Path(source_dir).resolve(strict=True)
    output = Path(output_root).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise NeedsReview("输出目录与原盘目录重叠")
    if (not isinstance(series, str) or not series.strip() or series.strip() in {".", ".."}
            or any(c in series for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in series)):
        raise NeedsReview("剧名含无效路径字符")
    if type(season) is not int or not 1 <= season <= 999:
        raise NeedsReview("需要明确的单一季号")
    if tmdb_id and not str(tmdb_id).isdigit():
        raise NeedsReview("TMDB 编号无效")
    if not isinstance(mapping, dict) or type(mapping.get("season")) is not int or mapping["season"] != season:
        raise NeedsReview("助手提交的季号与 MoviePilot 识别结果不一致")
    if mapping.get("unknown") != []:
        raise NeedsReview("助手仍有未确认的分集关系，本次不整理")
    basis = mapping.get("basis")
    evidence = mapping.get("evidence")
    episodes = mapping.get("episodes")
    if not isinstance(basis, str) or not basis.strip() or len(basis) > 12000:
        raise NeedsReview("助手没有提交有效的配对依据")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 1000:
        raise NeedsReview("助手没有提交盘内证据路径")
    evidence_hashes = {}
    for value in evidence:
        path = relative_file(value, source)
        if path.suffix.lower() in {".m2ts", ".ssif", ".iso"}:
            raise NeedsReview("需要菜单或导航证据，不能只提供视频文件")
        evidence_hashes[str(path.relative_to(source))] = hashlib.sha256(
            bounded_read(path, 32 * 1024 * 1024)).hexdigest()
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= 1000:
        raise NeedsReview("助手未提交完整分集列表")
    if any(not isinstance(row, dict) or type(row.get("episode")) is not int for row in episodes):
        raise NeedsReview("助手的全季集号无效")
    rows_in = sorted(episodes, key=lambda row: row["episode"])
    if [row["episode"] for row in rows_in] != list(range(1, len(rows_in) + 1)):
        raise NeedsReview("全季集号必须唯一连续，不能缺集或重复")
    if expected_episodes and [r["episode"] for r in rows_in] != list(expected_episodes):
        raise NeedsReview("助手集数与媒体服务提供的本季完整集表不一致")
    discs = discs_in(source)
    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        raise NeedsReview("未找到 ffprobe，无法核对完整视频范围")
    rows, seen_videos, counts = [], set(), {}
    previous_disc = 0
    for item in rows_in:
        number = item.get("disc")
        if type(number) is not int or number not in discs or number < previous_disc:
            raise NeedsReview("助手提交的盘号无效或全季集序与盘序矛盾")
        previous_disc = number
        disc = discs[number]
        title = item.get("title")
        if (not isinstance(title, str) or not title.strip() or len(title) > 1000
                or any(ord(c) < 32 for c in title)):
            raise NeedsReview("助手没有确认有效的单集标题")
        alternatives = item.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > 100:
            raise NeedsReview("地区播放列表无效")
        variants = []
        for value in [item.get("playlist")] + alternatives:
            path = relative_file(value, source)
            if path.parent != disc / "BDMV/PLAYLIST" or not re.fullmatch(r"\d{5}\.mpls", path.name, re.I):
                raise NeedsReview("播放列表不属于提交的实际光盘")
            data = bounded_read(path, 8 * 1024 * 1024)
            items = validate_subpaths(data)
            variants.append((path, items))
            evidence_hashes[str(path.relative_to(source))] = hashlib.sha256(data).hexdigest()
        playlist, clips = variants[0]
        if any(other != clips for _, other in variants):
            raise NeedsReview("地区版本的视频或区间不一致")
        if len(clips) != 1:
            raise NeedsReview("该集由多个片段组成，不能用完整文件硬链接代替拼接")
        clip = clips[0]
        video = inside(disc / "BDMV/STREAM" / (clip["clip"] + ".m2ts"), source)
        if not video.is_file() or str(video) in seen_videos:
            raise NeedsReview("多个集号指向同一视频或视频不存在")
        seen_videos.add(str(video))
        # The clip comes from the real MPLS, never from an assistant-supplied filename.
        if item.get("video") is not None and relative_file(item["video"], source) != video:
            raise NeedsReview("助手给出的视频与实际播放列表不一致")
        start, duration = probe_video(video, executable)
        if (not all(math.isfinite(v) for v in (start, duration))
                or abs(start - clip["in"]) > .5 or abs(start + duration - clip["out"]) > .5):
            raise NeedsReview("播放列表仅引用视频的一部分，不能硬链接整个文件充当该集")
        stat = video.stat()
        counts[number] = counts.get(number, 0) + 1
        rows.append({"episode": item["episode"], "disc": number,
                     "disc_episode": counts[number], "title": title.strip(),
                     "playlist": playlist.stem, "clip": clip["clip"],
                     "seconds": round(clip["out"] - clip["in"], 3),
                     "video": str(video), "size": stat.st_size,
                     "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino, "device": stat.st_dev})
    if set(counts) != set(discs):
        raise NeedsReview("助手未完成所有光盘的分集配对，停止整季整理")
    plan = {"format": "assistant-mapping-hardlink-v1", "source": str(source),
            "series": series.strip(), "season": season, "tmdb_id": str(tmdb_id),
            "output": str(output / series.strip()), "episodes": rows,
            "evidence": evidence_hashes, "navigation": [], "mapping_basis": basis.strip()}
    plan["fingerprint"] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return plan


def scan_with_agent(plugin, source_dir, series, season, output_root, tmdb_id="", should_stop=None, expected_episodes=None):
    from .agent_bridge import analyze
    source = Path(source_dir).resolve(strict=True)
    before = snapshot(source)
    key = "agent_mapping_" + hashlib.sha256(f"{source}\0{season}".encode()).hexdigest()
    failures = getattr(plugin, "_analysis_failures", None)
    if failures is None:
        failures = plugin._analysis_failures = {}
    previous = failures.get(key)
    if previous and previous[0] == before and time.monotonic() - previous[1] < 60:
        raise NeedsReview("同季预览刚刚未通过，暂不重复调用助手：" + previous[2])
    cached = plugin.get_data(key)
    mapping = cached.get("mapping") if isinstance(cached, dict) and cached.get("snapshot") == before else None
    try:
        if mapping is None:
            plugin.save_data("status", "正在调用智能助手分析整季蓝光；仅分析，不修改媒体")
            mapping = analyze(source, season, plugin._config.get("analysis_timeout", 1200), should_stop)
        if snapshot(source) != before:
            raise NeedsReview("分析期间源目录发生变化，请重新预览")
        # Preserve a failed candidate for diagnosis, never treat it as an accepted plan/cache.
        plugin.save_data("analysis_candidate", {"source": str(source), "season": season,
                         "snapshot": before, "mapping": mapping, "validated": False})
        plugin.save_data("status", "助手已提交配对，正在核对播放列表与完整视频范围")
        plan = checked_plan(source, series, season, output_root, mapping, tmdb_id,
                            expected_episodes=expected_episodes)
        if snapshot(source) != before:
            raise NeedsReview("分析期间源目录发生变化，请重新预览")
        if should_stop and should_stop():
            raise NeedsReview("整理已停止")
    except Exception as exc:
        failures[key] = (before, time.monotonic(), str(exc))
        raise
    failures.pop(key, None)
    plugin.save_data(key, {"snapshot": before, "mapping": mapping})
    plugin.save_data("analysis_candidate", {"source": str(source), "season": season,
                     "snapshot": before, "mapping": mapping, "validated": True})
    return plan
