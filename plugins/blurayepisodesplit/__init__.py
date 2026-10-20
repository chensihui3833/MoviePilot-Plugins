# -*- coding: utf-8 -*-
"""蓝光原盘分集插件。

接管 MoviePilot 整理链里的蓝光原盘目录：解析 BDMV/PLAYLIST/*.mpls，
判定这一盘里有几集、哪组 m2ts 对应第几集，然后每集一个目录地硬链接出去，
每个目录内是该集自己的 BDMV 结构。

本模块分两层：
- 纯逻辑层（plan_targets / build_episode_targets / execute_targets 等）不 import app.*，
  可以脱离 MoviePilot 单独调用，用于只读预览。
- 插件外壳层（BlurayEpisodeSplit）只做配置、日志、TransferInfo 组装。
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase

try:  # MoviePilot 以 app.plugins.blurayepisodesplit 加载插件
    from app.plugins.blurayepisodesplit import mpls, planner
except ImportError:  # pragma: no cover - 纯模块 / 单元测试加载
    try:
        from . import mpls, planner
    except ImportError:  # pragma: no cover
        import mpls
        import planner

class _TransferInfo:
    """app.schemas.TransferInfo 的一个退化替身。

    真实环境里 transfer() 拿到的是宿主用 model_construct 造出来的实例，
    与之同类型即可，不依赖具体字段；缺 pydantic 的纯逻辑环境里用它兜底。
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return "TransferInfo(%r)" % (self.__dict__,)


try:  # 两个宿主的 app.schemas 都再导出 TransferInfo
    from app.schemas import TransferInfo  # type: ignore
except Exception:  # pragma: no cover - 纯逻辑环境
    TransferInfo = _TransferInfo

# 同名字幕 / 音轨等附加文件的后缀，硬链接时一并带上
SIDECAR_SUFFIXES = (
    ".srt", ".ass", ".ssa", ".sup", ".vobsub", ".idx", ".sub", ".smi",
    ".nfo", ".txt",
)

# 整盘兜底结构里额外递归复制的目录：其余子树（BACKUP/AUXDATA/BDJO/JAR）可能指向
# 已被排除的 clip 或旧 playlist，整份拷过去会给出与内容不符的菜单
BDMV_FIXED_NAMES = ("index.bdmv", "MovieObject.bdmv")
BDMV_FIXED_SUBDIRS = ("PLAYLIST", "META")

# 硬链接临时后缀（两次尝试之间切换，确保互不冲突）
_LINK_TMP_SUFFIX = ".blurayepisodesplit.tmp"


class BluraySplitError(Exception):
    """单次整理失败，message 直接用于 TransferInfo.message。"""


@dataclass
class PlannedFile:
    """一个待落地的文件：源路径 + 目标路径。"""

    source: Path
    target: Path
    size: int = 0


@dataclass
class EpisodeTarget:
    """一集的落地计划：目标目录 + 该集自己的 BDMV 结构。"""

    kind: str = "episode"  # episode / bonus
    name: str = ""
    directory: Path = None
    episode_number: int = 0
    clips: list = field(default_factory=list)
    playlists: list = field(default_factory=list)
    files: list = field(default_factory=list)
    sidecars: list = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files) + sum(item.size for item in self.sidecars)


@dataclass
class SplitPlan:
    """整盘的计划。warnings 是给日志看的，不参与判定。"""

    root: Path = None
    episodes: list = field(default_factory=list)
    bonus: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    season: int = 0
    current: Any = None

    def all_targets(self) -> List[EpisodeTarget]:
        return list(self.episodes) + list(self.bonus)

    @property
    def total_size(self) -> int:
        return sum(target.size for target in self.all_targets())


# ------------------------------------------------------------------ 纯逻辑层
def resolve_config(config: Optional[dict]) -> dict:
    """把插件配置归一化成 planner 需要的秒数等参数，缺省值写死在这里。"""
    config = config or {}

    def _float(key, default):
        try:
            value = config.get(key)
            return default if value is None or value == "" else float(value)
        except (TypeError, ValueError):
            return default

    def _int(key, default):
        try:
            value = config.get(key)
            return default if value is None or value == "" else int(value)
        except (TypeError, ValueError):
            return default

    handling = str(config.get("bonus_handling") or "attach").strip().lower()
    if handling not in ("attach", "separate", "skip"):
        handling = "attach"

    return {
        "enabled": bool(config.get("enabled", False)),
        "notify": bool(config.get("notify", True)),
        "dry_run": bool(config.get("dry_run", False)),
        "bonus_handling": handling,
        "episode_offset": _int("episode_offset", 0),
        "min_episode_seconds": _float("min_episode_minutes", 10.0) * 60.0,
        "max_episode_seconds": _float("max_episode_minutes", 75.0) * 60.0,
        "min_bonus_seconds": _float("min_bonus_minutes", 5.0) * 60.0,
    }


def scan_runtime_minutes(episodes_info) -> Optional[float]:
    """取第一集的时长（分钟）。缺失时返回 None，planner 会自动跳过该判据。"""
    if not episodes_info:
        return None
    try:
        runtime = getattr(episodes_info[0], "runtime", None)
    except (IndexError, TypeError):
        return None
    try:
        runtime = float(runtime)
    except (TypeError, ValueError):
        return None
    return runtime if runtime > 0 else None


def count_large_clips(bluray_root, threshold=1024 ** 3) -> Optional[int]:
    """STREAM 下大于 threshold 的 m2ts 数量，仅用于交叉校验。"""
    try:
        stream = Path(bluray_root) / "BDMV" / "STREAM"
        if not stream.is_dir():
            return None
        total = 0
        for entry in stream.iterdir():
            try:
                if entry.is_file() and entry.suffix.lower() == ".m2ts" and entry.stat().st_size > threshold:
                    total += 1
            except OSError:
                continue
        return total
    except OSError:
        return None


def plan_targets(root, offset=0, season=None, runtime_minutes=None,
                 min_episode_seconds=600.0, max_episode_seconds=4500.0,
                 min_bonus_seconds=300.0) -> SplitPlan:
    """算出一张盘的分集计划。只读，不写盘。

    season 未给时从盘内元数据读取；只有当盘内也读不到、且兄弟盘 season 不唯一
    时回落到 0，避免把不确定的季号写进目录名。
    """
    root = Path(root)
    siblings = planner.discover_sibling_discs(root)
    if not siblings:
        siblings = [root]

    large = count_large_clips(root)
    plans = [
        planner.plan_disc(
            disc,
            min_episode_seconds=min_episode_seconds,
            max_episode_seconds=max_episode_seconds,
            runtime_minutes=runtime_minutes,
            min_bonus_seconds=min_bonus_seconds,
            large_clip_count=large if Path(disc) == root else None,
        )
        for disc in siblings
    ]
    planner.assign_episode_numbers(plans, offset=offset)

    current = next((item for item in plans if Path(item.root) == root), None)
    if current is None:
        raise BluraySplitError("无法在兄弟盘中定位当前盘：%s" % root)

    # 季号来源优先级：调用方 > 当前盘元数据 > 兄弟盘唯一季号 > 0（不确定就不写死）
    resolved_season = season if season is not None else current.season
    if resolved_season is None:
        seasons = {item.season for item in plans if item.season is not None}
        resolved_season = seasons.pop() if len(seasons) == 1 else 0

    return SplitPlan(
        root=root,
        episodes=list(current.episodes),
        bonus=list(current.bonus),
        warnings=list(current.warnings),
        resolved=[{
            "root": str(item.root),
            "disc_number": item.disc_number,
            "season": item.season,
            "episodes": [{"start_number": ep.start_number, "playlist": ep.playlist,
                          "clips": list(ep.clips), "duration": round(ep.duration, 1)}
                         for ep in item.episodes],
            "bonus": [{"playlist": bon.playlist, "clips": list(bon.clips),
                       "duration": round(bon.duration, 1)} for bon in item.bonus],
        } for item in plans],
        season=resolved_season,
        current=current,
    )


def _season_folder(season) -> str:
    try:
        season = int(season)
    except (TypeError, ValueError):
        season = 0
    return "Season %02d" % max(season, 0)


def _find_child_ci(directory: Path, name: str) -> Optional[Path]:
    """大小写不敏感的子项查找，兼容 Windows / Linux / SMB。"""
    direct = directory / name
    if direct.exists():
        return direct
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    target = name.lower()
    for entry in entries:
        if entry.name.lower() == target:
            return entry
    return None


def _safe_resolve(path: Path) -> Path:
    """resolve 失败时退回归一化后的原路径，仅用于集合比对。"""
    try:
        return path.resolve()
    except OSError:
        return path


def _collect_playlist_files(bdmv: Path, playlists, excluded) -> List[Path]:
    """要复制的 playlist 文件。

    能精确对应到某集/某个花絮的只放用到的那几个；一个都对应不上时（例如
    Episode.playlist 不是真实文件名）整份复制，绝不合成一个不存在的文件名。
    """
    playlist_dir = _find_child_ci(bdmv, "PLAYLIST")
    if playlist_dir is None or not playlist_dir.is_dir():
        return []
    try:
        entries = [entry for entry in playlist_dir.iterdir() if entry.is_file()]
    except OSError:
        return []
    wanted = {str(name).lower() for name in playlists if name}
    files = [entry for entry in entries if entry.name.lower() in wanted]
    if not files:
        files = entries
    return [entry for entry in files if Path(entry).resolve() not in excluded]


def _collect_fixed_files(bdmv: Path) -> List[Path]:
    files = []
    for name in BDMV_FIXED_NAMES:
        candidate = _find_child_ci(bdmv, name)
        if candidate is not None and candidate.is_file():
            files.append(candidate)
    return files


def _collect_tree(directory: Optional[Path]) -> List[Tuple[Path, Path]]:
    """目录递归展开成（源文件, 相对路径）。相对路径必须保留：

    CERTIFICATE/BACKUP 下有一整套与父目录同名的文件，压平会互相覆盖。
    """
    if directory is None or not directory.is_dir():
        return []
    collected = []
    try:
        for entry in sorted(directory.rglob("*")):
            try:
                if entry.is_file():
                    collected.append((entry, entry.relative_to(directory)))
            except (OSError, ValueError):
                continue
    except OSError:
        return collected
    return collected


def _sidecar_files(stream: Path, clip_names) -> List[Path]:
    """同一 clip 旁边的字幕/音轨等附加文件。"""
    if not clip_names:
        return []
    wanted = {name.lower() for name in clip_names}
    found = []
    try:
        for entry in stream.iterdir():
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            stem = entry.stem.lower()
            if stem not in wanted or entry.suffix.lower() == ".m2ts":
                continue
            if entry.suffix.lower() in SIDECAR_SUFFIXES:
                found.append(entry)
    except OSError:
        return found
    return sorted(found)


def build_episode_targets(plan: SplitPlan, destination, media_name="",
                          handling="attach", year=None) -> List[EpisodeTarget]:
    """把计划翻译成一串 EpisodeTarget。只拼路径，不碰磁盘目标端。"""
    root = Path(plan.root)
    destination = Path(destination)
    bdmv = _find_child_ci(root, "BDMV")
    if bdmv is None or not bdmv.is_dir():
        raise BluraySplitError("源目录缺少 BDMV：%s" % root)
    stream = _find_child_ci(bdmv, "STREAM")
    clipinf = _find_child_ci(bdmv, "CLIPINF")

    episodes = plan.current.episodes
    bonus = plan.current.bonus
    season = plan.season
    tags = list(root.parts[-6:])

    series = str(media_name or root.name).strip() or root.name
    if year:
        series = "%s (%s)" % (series, year)

    fixed_files = _collect_fixed_files(bdmv)
    meta_dir = _find_child_ci(bdmv, "META")
    cert_dir = _find_child_ci(root, "CERTIFICATE")

    # 已被某一集占用的 m2ts 不再当作花絮引用，playlist 也一并排除
    episode_excluded = set()
    for ep in episodes:
        for clip in ep.clips:
            if stream is not None:
                episode_excluded.add(_safe_resolve(stream / ("%s.m2ts" % clip)))

    def add_disc_scaffolding(target: EpisodeTarget, playlists, excluded) -> None:
        """给一个目标目录补齐盘级结构：固定文件、META、该目录用到的 playlist、CERTIFICATE。"""
        for item in fixed_files:
            target.files.append(PlannedFile(item, target.directory / "BDMV" / item.name))
        for item, relative in _collect_tree(meta_dir):
            target.files.append(PlannedFile(item, target.directory / "BDMV" / relative))
        for item in _collect_playlist_files(bdmv, playlists, excluded):
            target.files.append(PlannedFile(item, target.directory / "BDMV" / "PLAYLIST" / item.name))
        for item, relative in _collect_tree(cert_dir):
            target.files.append(PlannedFile(item, target.directory / "CERTIFICATE" / relative))

    def add_clip_payload(target: EpisodeTarget, clips) -> None:
        """该目标用到的 m2ts / clpi / 同名字幕音轨。"""
        for clip in clips:
            if stream is not None:
                target.files.append(PlannedFile(stream / ("%s.m2ts" % clip),
                                                target.directory / "BDMV" / "STREAM" / ("%s.m2ts" % clip)))
            if clipinf is not None:
                target.files.append(PlannedFile(clipinf / ("%s.clpi" % clip),
                                                target.directory / "BDMV" / "CLIPINF" / ("%s.clpi" % clip)))
        if stream is not None:
            target.sidecars = [PlannedFile(item, target.directory / "BDMV" / "STREAM" / item.name)
                               for item in _sidecar_files(stream, clips)]

    targets: List[EpisodeTarget] = []
    for ep in episodes:
        label = "E%02d" % max(int(ep.start_number or 0), 0)
        directory = destination / _season_folder(season) / ("%s - %s" % (series, label))
        target = EpisodeTarget(
            kind="episode",
            name=label,
            directory=directory,
            episode_number=int(ep.start_number or 0),
            clips=list(ep.clips),
            playlists=[ep.playlist] if ep.playlist else [],
        )
        add_clip_payload(target, ep.clips)
        add_disc_scaffolding(target, target.playlists, episode_excluded)
        targets.append(target)

    if handling == "skip" or not bonus:
        # 无花絮的盘也要补齐字节数，否则 TransferInfo.total_size 会是 0
        fill_sizes(targets)
        return targets

    bonus_targets: List[EpisodeTarget] = []
    for position, item in enumerate(bonus, start=1):
        if handling == "attach" and targets:
            directory = targets[0].directory
            name = targets[0].name
        else:
            label = "Bonus %d" % position
            directory = destination / _season_folder(season) / ("%s - %s" % (series, label))
            name = label
        target = EpisodeTarget(kind="bonus", name=name, directory=directory,
                               clips=list(item.clips),
                               playlists=[item.playlist] if item.playlist else [])
        add_clip_payload(target, item.clips)
        if handling == "separate":
            add_disc_scaffolding(target, target.playlists, set())
        elif targets:
            # attach：只把花絮 playlist 追加进首个分集目录，其余结构已在那儿
            for entry in _collect_playlist_files(bdmv, target.playlists, set()):
                target.files.append(PlannedFile(entry, targets[0].directory / "BDMV" / "PLAYLIST" / entry.name))
        bonus_targets.append(target)

    all_targets = targets + bonus_targets
    fill_sizes(all_targets)
    return all_targets


def fill_sizes(targets) -> None:
    """补齐每个待落地文件的字节数；EpisodeTarget.size 是只读属性，由 files/sidecars 汇总。"""
    for target in targets:
        for item in list(target.files) + list(target.sidecars):
            if item.size:
                continue
            try:
                item.size = int(item.source.stat().st_size)
            except OSError:
                item.size = 0


def check_plan(plan: SplitPlan, targets) -> List[str]:
    """落地前校验源文件存在，返回问题清单（空表示可执行）。"""
    problems = []
    for target in targets:
        for item in list(target.files) + list(target.sidecars):
            if not item.source.exists():
                problems.append("源文件不存在：%s" % item.source)
    return problems


def verify_targets(targets) -> List[str]:
    """落地后核对：m2ts 的字节数与源一致（硬链接必然一致，这里防的是复制错源）。"""
    problems = []
    for target in targets:
        for item in target.files:
            if item.source.suffix.lower() != ".m2ts":
                continue
            try:
                if item.target.stat().st_size != item.source.stat().st_size:
                    problems.append("大小不一致：%s" % item.target)
            except OSError as exc:
                problems.append("无法校验 %s：%s" % (item.target, exc))
    return problems


def hardlink_file(source: Path, target: Path) -> None:
    """硬链接。同卷直接建链，跨卷才退回复制。"""
    try:
        target.hardlink_to(source)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if getattr(exc, "errno", None) not in (1, 17, 18, 22, 95):
            raise
    first, second = target.with_name(target.name + _LINK_TMP_SUFFIX), target.with_name(target.name + ".tmp")
    last_error = None
    for staging in (first, second):
        try:
            if staging.exists():
                staging.unlink()
            try:
                staging.hardlink_to(source)
            except OSError:
                shutil.copy2(str(source), str(staging))
            shutil.move(str(staging), str(target))
            return
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def execute_targets(targets, transfer_type="link", overwrite=False):
    """按计划落地。返回（已处理文件列表, 目标文件列表, 失败清单, 总字节数）。

    transfer_type 不是 link/copy 时只建目录不搬文件：移动会破坏源盘，
    而源盘还要靠它给其他盘/其他文件做链接。
    """
    transferred, created, failed = [], [], []
    total = 0
    seen_dirs = set()
    for target in targets:
        if target.directory not in seen_dirs:
            target.directory.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(target.directory)
        for item in list(target.files) + list(target.sidecars):
            try:
                item.target.parent.mkdir(parents=True, exist_ok=True)
                if item.target.exists():
                    if not overwrite:
                        continue
                    item.target.unlink()
                if transfer_type == "link":
                    hardlink_file(item.source, item.target)
                elif transfer_type == "copy":
                    shutil.copy2(str(item.source), str(item.target))
            except OSError as exc:
                logger.error("蓝光分集：落地 %s 失败 - %s" % (item.target, exc))
                failed.append(str(item.source))
                continue
            transferred.append(str(item.source))
            created.append(str(item.target))
            total += item.size
    return transferred, created, failed, total


def preview_report(plan: SplitPlan, targets, limit=12) -> Dict[str, Any]:
    """预览用的可序列化结果。"""
    episodes, bonus = [], []
    for target in targets:
        record = {
            "name": target.name,
            "kind": target.kind,
            "directory": str(target.directory),
            "episode_number": target.episode_number,
            "clips": list(target.clips),
            "playlists": [item.target.name for item in target.files
                          if item.target.parent.name.lower() == "playlist"],
            "files": [str(item.source) for item in target.files],
            "sidecars": [str(item.source) for item in target.sidecars],
            "size": target.size,
        }
        (episodes if target.kind == "episode" else bonus).append(record)
    return {
        "root": str(plan.root),
        "discs": plan.resolved,
        "warnings": plan.warnings[:limit],
        "episodes": episodes,
        "bonus": bonus,
        "total_size": plan.total_size,
    }


class BlurayEpisodeSplit(_PluginBase):
    # 插件名称
    plugin_name = "蓝光原盘分集"
    # 插件描述
    plugin_desc = "把蓝光原盘电视剧按集拆开整理：解析 mpls 判定每盘几集，每集一个目录硬链接出去"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/bluray.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "presi"
    # 作者主页
    author_url = "https://github.com/presi"
    # 插件配置项ID前缀
    plugin_config_prefix = "blurayepisodesplit_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 2
    # 是否为插件分身
    is_clone = False

    def init_plugin(self, config: dict = None):
        self._config = resolve_config(config)
        self._enabled = self._config["enabled"]
        if config is not None and not config:
            self.update_config({
                "enabled": False,
                "notify": True,
                "min_episode_minutes": 10,
                "max_episode_minutes": 75,
                "min_bonus_minutes": 5,
                "episode_offset": 0,
                "bonus_handling": "attach",
                "dry_run": False,
            })
        self._preview_data = {}

    def get_state(self) -> bool:
        return bool(self._enabled)

    def get_module(self) -> Dict[str, Any]:
        return {"transfer": self.transfer}

    # ------------------------------------------------------------ 整理入口
    def transfer(self, fileitem, meta, mediainfo, target_directory=None, target_storage=None,
                 target_path=None, transfer_type=None, scrape=None, library_type_folder=None,
                 library_category_folder=None, episodes_info=None, source_oper=None,
                 target_oper=None, preview=False) -> Optional[TransferInfo]:
        """接管蓝光原盘的整理。返回 None 表示不接管，交回 MoviePilot 原生整理。"""
        try:
            source_path = getattr(fileitem, "path", None)
            if not source_path:
                return None
            source_storage = getattr(fileitem, "storage", None) or "local"
            # 非本地源（网盘/远程存储）没有硬链接语义，交给原生整理
            if source_storage != "local":
                return None
            root = mpls.find_bluray_root(source_path)
            if root is None:
                return None

            config = self._config
            runtime_minutes = scan_runtime_minutes(episodes_info)
            plan = plan_targets(
                root,
                offset=config["episode_offset"],
                runtime_minutes=runtime_minutes,
                min_episode_seconds=config["min_episode_seconds"],
                max_episode_seconds=config["max_episode_seconds"],
                min_bonus_seconds=config["min_bonus_seconds"],
            )
            if not plan.episodes:
                logger.info("蓝光分集：%s 未能判定出分集，交回原生整理" % root)
                return None

            media_name = getattr(getattr(mediainfo, "title", None), "__str__", lambda: "")() or ""
            year = getattr(mediainfo, "year", None) if mediainfo else None
            destination = Path(target_path) if target_path else (Path(target_directory) if target_directory else None)
            if destination is None:
                logger.error("蓝光分集：缺少目标目录，交回原生整理")
                return None

            targets = build_episode_targets(plan, destination, media_name=media_name,
                                            handling=config["bonus_handling"], year=year)
            problems = check_plan(plan, targets)
            if problems:
                return TransferInfo(
                    success=False,
                    message="蓝光分集校验失败：%s" % "；".join(problems[:5]),
                    fileitem=fileitem,
                    fail_list=[str(item) for item in problems[:5]],
                    transfer_type=transfer_type,
                    need_notify=config["notify"],
                )

            if preview or config["dry_run"]:
                return self._preview_result(fileitem, plan, targets, transfer_type, config, bool(preview))

            if transfer_type not in (None, "link", "copy"):
                # 移动会毁掉源盘，直接交回原生整理
                logger.warn("蓝光分集：整理方式 %s 不支持，交回原生整理" % transfer_type)
                return None

            transferred, created, failed, total = execute_targets(
                targets, transfer_type=transfer_type or "link")
            if failed:
                return TransferInfo(
                    success=False,
                    message="蓝光分集：%d 个文件落地失败" % len(failed),
                    fileitem=fileitem,
                    fail_list=failed[:20],
                    transfer_type=transfer_type,
                    need_notify=config["notify"],
                )

            target_item = getattr(fileitem, "model_copy", lambda **_: fileitem)()
            target_item.path = str(destination)
            target_item.name = destination.name
            target_item.basename = destination.name
            target_item.type = "dir"
            target_item.extension = None
            target_item.storage = getattr(fileitem, "storage", "local")
            target_item.children = []

            logger.info("蓝光分集：%s 落地 %d 集（花絮 %d 项），共 %d 个文件 %.2f GiB"
                        % (root.name, len([t for t in targets if t.kind == "episode"]),
                           len([t for t in targets if t.kind == "bonus"]), len(created), total / 1024 ** 3))

            return TransferInfo(
                success=True,
                fileitem=fileitem,
                target_item=target_item,
                target_diritem=target_item,
                need_scrape=True,
                need_notify=config["notify"],
                transfer_type=transfer_type or "link",
                file_list=transferred,
                file_list_new=created,
                file_count=len(created),
                total_size=total,
            )
        except BluraySplitError as exc:
            logger.error("蓝光分集：%s" % exc)
            return None
        except Exception as exc:
            logger.error("蓝光分集：整理异常 %s - %s" % (type(exc).__name__, exc))
            return None

    def _preview_result(self, fileitem, plan, targets, transfer_type, config, is_preview):
        keep = ""
        report = preview_report(plan, targets)
        self._preview_data = report
        logger.info("蓝光分集：预览 %s 命中 %d 集、花絮 %d 项，共 %.2f GiB%s"
                    % (plan.root.name, len(report["episodes"]), len(report["bonus"]),
                       report["total_size"] / 1024 ** 3, keep))
        message = "预览：%d 集 / 花絮 %d 项 / %.2f GiB" % (
            len(report["episodes"]), len(report["bonus"]), report["total_size"] / 1024 ** 3)
        return TransferInfo(
            success=True,
            message=message,
            fileitem=fileitem,
            transfer_type=transfer_type,
            need_scrape=not is_preview,
            need_notify=False,
            file_count=0,
            total_size=0,
        )

    # -------------------------------------------------------------- 插件页面
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "dry_run", "label": "只算不搬"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_episode_minutes",
                                            "label": "单集最短时长（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_episode_minutes",
                                            "label": "单集最长时长（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_bonus_minutes",
                                            "label": "花絮最短时长（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "episode_offset",
                                            "label": "集号偏移",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "bonus_handling",
                                            "label": "花絮处理",
                                            "items": [
                                                {"title": "并入首个分集目录", "value": "attach"},
                                                {"title": "单独一个目录", "value": "separate"},
                                                {"title": "不处理", "value": "skip"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {"type": "info", "variant": "tonal"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "text": "仅在整理方式为硬链接时生效；每集一个目录，目录内是该集自己的 BDMV 结构。",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "min_episode_minutes": 10,
            "max_episode_minutes": 75,
            "min_bonus_minutes": 5,
            "episode_offset": 0,
            "bonus_handling": "attach",
            "dry_run": False,
        }

    def get_page(self) -> List[dict]:
        data = getattr(self, "_preview_data", None) or {}
        episodes = data.get("episodes") or []
        if not episodes:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {"class": "text-center"},
                }
            ]
        rows = [
            {
                "component": "VTable",
                "props": {"density": "compact"},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "th", "text": "集"},
                                    {"component": "th", "text": "clip"},
                                    {"component": "th", "text": "目标目录"},
                                ],
                            }
                        ],
                    },
                    {
                        "component": "tbody",
                        "content": [
                            {
                                "component": "tr",
                                "content": [
                                    {"component": "td", "text": str(item.get("name", ""))},
                                    {"component": "td", "text": "/".join(item.get("clips") or [])},
                                    {"component": "td", "text": str(item.get("directory", ""))},
                                ],
                            }
                            for item in episodes
                        ],
                    },
                ],
            }
        ]
        return rows

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/preview",
                "endpoint": self.api_preview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "只读预览指定蓝光目录的分集结果",
            }
        ]

    def api_preview(self, path: str = "") -> dict:
        """只读预览：给一个蓝光目录，返回分集结果，不写盘。"""
        try:
            root = mpls.find_bluray_root(path)
            if root is None:
                return {"success": False, "message": "不是蓝光目录：%s" % path}
            config = self._config
            plan = plan_targets(
                root,
                offset=config["episode_offset"],
                min_episode_seconds=config["min_episode_seconds"],
                max_episode_seconds=config["max_episode_seconds"],
                min_bonus_seconds=config["min_bonus_seconds"],
            )
            if not plan.episodes:
                return {"success": False, "message": "未能判定出分集", "data": plan.resolved}
            targets = build_episode_targets(plan, Path(path) / "_preview",
                                            handling=config["bonus_handling"])
            return {"success": True, "data": preview_report(plan, targets)}
        except Exception as exc:
            return {"success": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def stop_service(self):
        pass
