"""MoviePilot 3.1.5/3.1.6 transfer provider; no patches to host code."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from .core import NeedsReview, export_plan, scan_season
from .agent_mapping import scan_with_agent
from .disc_layout import disc_number


def prepare_native_plan(fileitem, meta, mediainfo, destination, recommend_name,
                        episodes_info=None, scanner=None):
    disc = Path(fileitem.path).resolve(strict=True)
    number = disc_number(disc)
    if number is None:
        raise NeedsReview("无法确认原盘编号，请核对目录或盘内身份信息")
    if not getattr(meta, "begin_season", None) or getattr(meta, "end_season", None):
        raise NeedsReview("未明确识别单一季号，请在 MoviePilot 手动整理中指定季号")
    destination = Path(destination).resolve()
    series = mediainfo.title_year
    plan = (scanner or scan_season)(
        disc.parent, series, meta.begin_season, destination,
        tmdb_id=getattr(mediainfo, "tmdb_id", None) or "",
    )
    # The host's naming template supplies the show/season directories and basename.
    # Validate every episode's destination, including other discs, before filtering.
    seen = set()
    for row in plan["episodes"]:
        episode_meta = deepcopy(meta)
        episode_meta.title = Path(row["video"]).name
        episode_meta.begin_episode = row["episode"]
        episode_meta.end_episode = None
        episode_meta.total_episode = 1
        episode_meta.end_season = None
        episode_meta.total_season = 1
        episode_meta.part = None
        names = deepcopy(episodes_info or [])
        for info in names:
            if info.episode_number == row["episode"]:
                info.name = row["title"]
        relative = recommend_name(meta=episode_meta, mediainfo=mediainfo, episodes_info=names)
        if not relative:
            raise NeedsReview("MoviePilot 命名规则没有生成文件路径")
        relative = Path(relative)
        if (relative.is_absolute() or ".." in relative.parts
                or relative.suffix.lower() != ".m2ts"
                or len(relative.parts) < 2
                or not (destination / relative).resolve().is_relative_to(destination)):
            raise NeedsReview("MoviePilot 命名规则产生无效或越界路径")
        # Host/Emby must be able to recognize each file independently.
        marker = f"S{plan['season']:02}E{row['episode']:02}"
        if marker.casefold() not in relative.name.casefold():
            raise NeedsReview("命名规则的文件名必须包含 SxxExx 季集号")
        key = relative.as_posix().casefold()
        if key in seen:
            raise NeedsReview("MoviePilot 命名规则把不同集映射到同一文件")
        seen.add(key)
        row["relative"] = relative.as_posix()
    plan["episodes"] = [r for r in plan["episodes"] if r["disc"] == number]
    if not plan["episodes"]:
        raise NeedsReview("整季映射中没有当前盘")
    plan["output"] = str(destination)
    # Existing show NFO belongs to the host; never replace it.
    plan["write_tvshow"] = False
    plan["preserve_enriched_nfo"] = True
    plan["mapping_basis"] += "；沿用 MoviePilot 命名"
    plan["fingerprint"] = hashlib.sha256(
        json.dumps({k: v for k, v in plan.items() if k != "fingerprint"},
                   sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return plan


def require_execution_allowed(plugin):
    # The host may retain an older bound provider after a config reload.
    # Read the persisted value at the actual write boundary, never its stale snapshot.
    try:
        config = plugin.get_config()
    except Exception as exc:
        raise NeedsReview("无法读取插件最新执行配置，停止媒体写入") from exc
    if not isinstance(config, dict):
        raise NeedsReview("插件最新执行配置无效，停止媒体写入")
    if config.get("preview_only", True) is not False:
        raise NeedsReview("当前为仅预览模式；请先在文件管理中预览核对，再关闭插件的仅预览开关")


def native_transfer(plugin, fileitem, meta, mediainfo, target_directory=None,
                    target_storage=None, target_path=None, transfer_type=None,
                    scrape=None, library_type_folder=None, library_category_folder=None,
                    episodes_info=None, source_oper=None, target_oper=None, preview=False):
    from app.schemas.transfer import TransferInfo
    from app.schemas.file import FileItem
    from app.schemas.types import MediaType

    if (not plugin.get_state() or getattr(mediainfo, "type", None) != MediaType.TV
            or fileitem.type != "dir" or fileitem.storage not in (None, "local")):
        return None
    disc = Path(fileitem.path)
    if not (disc / "BDMV").is_dir():
        return None
    # Once claimed, all errors become explicit failures. Never fall back to whole-disc transfer.
    try:
        if not preview:
            require_execution_allowed(plugin)
        from app.modules.filemanager.transhandler import TransHandler
        from app.application.directory import DirectoryHelper
        from app.runtime.settings import get_runtime_setting
        from app.runtime.stop import runtime_stop_state

        resolved_storage = target_storage or (
            target_directory.library_storage if target_directory else "local"
        )
        if resolved_storage not in (None, "local"):
            raise NeedsReview("蓝光分集仅支持本地同一文件系统的硬链接")
        if target_directory:
            if not target_directory.library_path or not target_directory.renaming:
                raise NeedsReview("目标目录必须启用重命名并配置媒体库路径")
            destination = TransHandler.get_dest_dir(
                mediainfo, target_directory, library_type_folder, library_category_folder,
            )
        elif target_path:
            destination = TransHandler.get_dest_path(
                mediainfo, Path(target_path), library_type_folder, library_category_folder,
            )
        else:
            raise NeedsReview("MoviePilot 没有匹配到目标目录")
        with plugin._run_lock:
            def stopped():
                return (not plugin.get_state() or runtime_stop_state.is_system_stopped
                        or runtime_stop_state.consume_transfer_stop(fileitem.path))

            if stopped():
                raise NeedsReview("整理已停止")
            expected = sorted({info.episode_number for info in (episodes_info or [])
                               if type(getattr(info, "episode_number", None)) is int
                               and info.episode_number > 0})
            if expected != list(range(1, len(expected) + 1)):
                expected = None
            plan = prepare_native_plan(
                fileitem, meta, mediainfo, destination,
                lambda **kwargs: plugin.chain.run_module("recommend_name", **kwargs),
                episodes_info,
                scanner=lambda *args, **kwargs: scan_with_agent(
                    plugin, *args, **kwargs, should_stop=stopped, expected_episodes=expected),
            )
            # Resolve the same media root used by the native naming template before writing.
            paths = [Path(plan["output"]) / row["relative"] for row in plan["episodes"]]
            show_root = DirectoryHelper.get_media_root_path(
                get_runtime_setting("RENAME_FORMAT")(mediainfo.type),
                rename_path=paths[0], media_type=mediainfo.type,
            )
            if not show_root or not Path(show_root).resolve().is_relative_to(Path(plan["output"])):
                raise NeedsReview("无法从 MoviePilot 命名规则确定剧集目录")
            if not preview:
                require_execution_allowed(plugin)
            result = export_plan(plan, plan["fingerprint"], preview=preview, should_stop=stopped)
            if preview:
                plugin._preview_plans[fileitem.path] = plan
            plugin.save_data("preview", plan)
            numbers = "、".join(str(row["episode"]) for row in plan["episodes"])
            message = f"{'预览通过' if preview else '分集完成'}：第 {numbers} 集；{result['episodes']} 个视频硬链接"
            plugin.save_data("status", message)
            return TransferInfo(
                success=True, fileitem=fileitem, transfer_type="link",
                target_diritem=FileItem(storage="local", path=str(show_root), type="dir",
                                        name=Path(show_root).name),
                # A disc maps to many files. Do not register the entire season as deletable.
                target_item=FileItem(storage="local", path=str(paths[0]), type="file",
                                     name=paths[0].name, extension="m2ts",
                                     size=plan["episodes"][0]["size"]),
                file_count=len(paths), file_list=[row["video"] for row in plan["episodes"]],
                file_list_new=[str(path) for path in paths],
                total_size=sum(row["size"] for row in plan["episodes"]),
                need_scrape=bool(scrape if scrape is not None else (
                    target_directory.scraping if target_directory else False)),
                need_notify=bool(target_directory and target_directory.notify),
                message=message,
            )
    except Exception as exc:
        message = f"蓝光分集待核对：{type(exc).__name__}: {exc}"
        plugin.save_data("status", message)
        return TransferInfo(success=False, fileitem=fileitem, transfer_type="link",
                            message=message, fail_list=[fileitem.path],
                            failure_stage="bluray_episodes",
                            recovery_action="核对完整季、智能助手配对、命名规则和目标目录后重新整理")
