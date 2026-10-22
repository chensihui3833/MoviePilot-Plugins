"""Scoped compatibility adapter for MoviePilot 3.1.5/3.1.6 disc names."""

import re
from functools import wraps
from pathlib import Path

from .disc_layout import disc_number


def install(plugin, planner_class=None):
    if planner_class is None:
        from app.chain.transfer.request import _TransferCandidatePlanner
        planner_class = _TransferCandidatePlanner
    original = planner_class._build_path_meta
    # A plugin reload must not stack adapters from its previous instance.
    original = getattr(original, "_blurayepisodes_original", original)

    @wraps(original)
    def build(planner, source_path, custom_word_list=None, force_video=False, *, storage="local"):
        path = Path(source_path)
        if (plugin.get_state() and storage in (None, "local")
                and disc_number(path) is not None
                and (path / "BDMV").is_dir()):
            from app.schemas.types import MediaType
            parent_meta = original(
                planner, path.parent, custom_word_list, force_video, storage=storage,
            )
            if (parent_meta and parent_meta.type == MediaType.TV
                    and parent_meta.begin_season is not None
                    and not parent_meta.end_season):
                return parent_meta
        return original(planner, source_path, custom_word_list, force_video, storage=storage)

    build._blurayepisodes_original = original
    planner_class._build_path_meta = build

    def restore():
        if planner_class._build_path_meta is build:
            planner_class._build_path_meta = original

    return restore
