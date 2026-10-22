"""Create real per-episode native tasks before MoviePilot durable admission."""
from copy import deepcopy
from functools import wraps
from pathlib import Path
from .agent_mapping import scan_source_with_agent
from .core import NeedsReview
from .disc_layout import disc_number
from .episode_source import source_location


def episode_meta(parent, row):
    value = deepcopy(parent)
    value.begin_episode = row["episode"]
    value.end_episode = None
    value.total_episode = 1
    value.end_season = None
    value.total_season = 1
    value.part = None
    return value


def reliable_meta(meta):
    from app.schemas.types import MediaType
    return (meta is not None and meta.type == MediaType.TV
            and type(meta.begin_season) is int and 1 <= meta.begin_season <= 999
            and not meta.end_season)


def source_plan(plugin, planner, disc, parent):
    def stopped():
        callback = getattr(planner, "_continue_callback", None)
        return not plugin.get_state() or (callback is not None and not callback())
    if stopped():
        raise NeedsReview("逐集规划已停止")
    with plugin._run_lock:
        return scan_source_with_agent(plugin, disc.parent, parent.begin_season, should_stop=stopped)


def make_file(row):
    from app.schemas.file import FileItem
    path = Path(row["video"])
    stat = path.stat()
    return FileItem(storage="local", type="file", path=str(path), name=path.name,
                    basename=path.stem, extension="m2ts", size=stat.st_size,
                    modify_time=stat.st_mtime)


def install(plugin, planner_class=None):
    if planner_class is None:
        from app.chain.transfer.request import _TransferCandidatePlanner
        planner_class = _TransferCandidatePlanner
    original_plan = getattr(planner_class._plan_file_items, "_blurayepisodes_original",
                            planner_class._plan_file_items)
    original_context = getattr(planner_class._build_file_context, "_blurayepisodes_original",
                               planner_class._build_file_context)

    @wraps(original_plan)
    def plan(planner, items):
        planned, inherited = original_plan(planner, items)
        if not plugin.get_state():
            return planned, inherited
        result, seen = [], set()
        known = getattr(planner, "_bluray_episode_meta", None)
        if known is None:
            known = planner._bluray_episode_meta = {}
        errors = getattr(plugin, "_episode_planning_errors", None)
        if errors is None:
            errors = plugin._episode_planning_errors = {}
        for item, bluray in planned:
            key = planner._chain._get_file_key(item)
            located = source_location(item)
            if located is None or located[1] is not None:
                if key not in seen:
                    result.append((item, bluray)); seen.add(key)
                continue
            disc, _ = located
            parent = planner._build_file_meta(item)
            if not reliable_meta(parent):
                if key not in seen:
                    result.append((item, bluray)); seen.add(key)
                continue
            try:
                full = source_plan(plugin, planner, disc, parent)
                rows = [r for r in full["episodes"] if r["disc"] == disc_number(disc)]
                if not rows:
                    raise NeedsReview("整季映射中没有当前盘")
                for row in rows:
                    file = make_file(row)
                    file_key = planner._chain._get_file_key(file)
                    meta = episode_meta(parent, row)
                    if file_key not in seen:
                        result.append((file, False)); seen.add(file_key)
                    known[file_key] = deepcopy(meta)
                    inherited[file_key] = deepcopy(meta)
                errors.pop(str(disc), None)
            except NeedsReview as exc:
                # No invented episode numbers when analysis itself is unresolved.
                errors[str(disc)] = str(exc)
                if key not in seen:
                    result.append((item, bluray)); seen.add(key)
        return result, inherited

    @wraps(original_context)
    def context(planner, fileitem, history, inherited, discard_saved_identity):
        if not plugin.get_state():
            return original_context(planner, fileitem, history, inherited, discard_saved_identity)
        key = planner._chain._get_file_key(fileitem)
        known = getattr(planner, "_bluray_episode_meta", {})
        if key in known:
            return deepcopy(known[key]), None
        located = source_location(fileitem)
        if located is None or located[1] is None:
            return original_context(planner, fileitem, history, inherited, discard_saved_identity)
        disc, video = located
        parent = deepcopy(getattr(planner, "_meta", None))
        if parent is None:
            parent = planner._build_path_meta(disc, storage=fileitem.storage)
        if not reliable_meta(parent):
            raise NeedsReview("蓝光视频没有可靠的单季电视剧身份")
        try:
            full = source_plan(plugin, planner, disc, parent)
        except NeedsReview as exc:
            # Let the claimed native provider return a durable explicit failure instead of HTTP500.
            plugin.save_data("status", "蓝光逐集身份待核对：" + str(exc))
            return parent, None
        rows = [r for r in full["episodes"] if Path(r["video"]) == video]
        if len(rows) != 1:
            parent.begin_episode = None
            parent.end_episode = None
            return parent, None
        explicit = getattr(planner, "_meta", None)
        if explicit is not None and (getattr(explicit, "begin_episode", None) not in (None, rows[0]["episode"])
                                     or getattr(explicit, "end_episode", None)):
            return parent, None  # Native source/episode validation records the mismatch as failure.
        return episode_meta(parent, rows[0]), None

    plan._blurayepisodes_original = original_plan
    context._blurayepisodes_original = original_context
    planner_class._plan_file_items = plan
    planner_class._build_file_context = context

    def restore():
        if planner_class._plan_file_items is plan:
            planner_class._plan_file_items = original_plan
        if planner_class._build_file_context is context:
            planner_class._build_file_context = original_context
    return restore
