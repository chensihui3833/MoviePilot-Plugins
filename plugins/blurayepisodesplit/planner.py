# -*- coding: utf-8 -*-
"""Blu-ray episode planning: turn a disc's MPLS playlists into episodes + bonus.

Pure functions only: no file writes, no network, no stat of large STREAM files.
The MPLS parsing layer lives in mpls.py; this module only consumes PlaylistInfo
objects.

The classification rules below were validated on a real 3-disc set
(Preacher S01, 570 mpls): 10/10 episodes correct.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re

try:  # MoviePilot loads plugins as app.plugins.<name>.planner
    from app.plugins.blurayepisodesplit.mpls import (
        MplsError,
        PlaylistInfo,
        find_bluray_root,
        read_disc_meta,
        scan_playlists,
    )
except ImportError:  # pragma: no cover - plain-module / unit-test loading
    try:
        from .mpls import (
            MplsError,
            PlaylistInfo,
            find_bluray_root,
            read_disc_meta,
            scan_playlists,
        )
    except ImportError:
        from mpls import (
            MplsError,
            PlaylistInfo,
            find_bluray_root,
            read_disc_meta,
            scan_playlists,
        )

__all__ = [
    "Episode",
    "BonusItem",
    "DiscPlan",
    "plan_disc",
    "discover_sibling_discs",
    "assign_episode_numbers",
]

_DISC_NUMBER_RE = re.compile(r"(?:disc|disk|cd|dvd)[\s._-]*(\d+)", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"(\d+)\s*$")


@dataclass
class Episode:
    """One episode found on a single disc."""

    index: int = 0
    start_number: int = 0
    clips: list = field(default_factory=list)
    playlist: str = ""
    duration: float = 0.0


@dataclass
class BonusItem:
    """One extra / bonus feature found on a single disc."""

    clips: list = field(default_factory=list)
    playlist: str = ""
    duration: float = 0.0


@dataclass
class DiscPlan:
    """Everything the planner decided about one disc."""

    root: Path = None
    disc_number: int = None
    season: int = None
    episodes: list = field(default_factory=list)
    bonus: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "root": str(self.root) if self.root is not None else None,
            "disc_number": self.disc_number,
            "season": self.season,
            "episodes": [
                {
                    "index": ep.index,
                    "start_number": ep.start_number,
                    "clips": list(ep.clips),
                    "playlist": ep.playlist,
                    "duration": ep.duration,
                }
                for ep in self.episodes
            ],
            "bonus": [
                {
                    "clips": list(b.clips),
                    "playlist": b.playlist,
                    "duration": b.duration,
                }
                for b in self.bonus
            ],
            "warnings": list(self.warnings),
        }


def _clip_key(playlist):
    """Identity of a candidate: its clip set, order-insensitive."""
    return tuple(sorted(playlist.clips))


def _order_key(playlist):
    """Sort key. Clip names only -- duration ordering is provably wrong."""
    first = playlist.clips[0] if playlist.clips else ""
    return (tuple(sorted(playlist.clips)), first)


def plan_disc(
    bluray_root,
    *,
    min_episode_seconds=600.0,
    max_episode_seconds=4500.0,
    runtime_minutes=None,
    playlists=None,
    min_bonus_seconds=300.0,
    large_clip_count=None,
):
    """Classify one disc's playlists into episodes and bonus items.

    runtime_minutes is the TMDB runtime; when None the runtime rule is skipped
    entirely (TMDB frequently has no runtime, and dropping episodes because of
    that would be silent data loss).

    large_clip_count is the number of >1 GiB m2ts files, supplied by the caller.
    It is used only for a cross-check warning -- never to decide episodes, and
    this function never stats STREAM itself.
    """
    root = Path(bluray_root)

    if playlists is None:
        bdmv = root / "BDMV"
        if not bdmv.is_dir():
            found = find_bluray_root(root)
            if found is not None:
                root = Path(found)
                bdmv = root / "BDMV"
        playlists = scan_playlists(bdmv)

    plan = DiscPlan(root=root)
    warnings = plan.warnings

    try:
        meta = read_disc_meta(root)
    except Exception as exc:  # metadata is optional, never fatal
        meta = {}
        warnings.append("disc metadata unavailable: %s" % (exc,))
    if meta:
        plan.disc_number = meta.get("disc_number")
        plan.season = meta.get("season")

    # Rule 1: a real episode is exactly one playitem. Multi-playitem playlists
    # are play-alls / menu compilations, and their runtime can land inside the
    # episode window, so duration alone cannot reject them.
    single = [pl for pl in playlists if pl.playitem_count == 1]
    skipped_multi = len(playlists) - len(single)
    if skipped_multi:
        warnings.append(
            "excluded %d playlist(s) with more than one playitem (play-all / menu)"
            % skipped_multi
        )

    # Rules 2 + 3: duration window, optionally tightened by the TMDB runtime.
    candidates = []
    for pl in single:
        if not (min_episode_seconds <= pl.duration <= max_episode_seconds):
            continue
        if runtime_minutes is not None:
            low = 0.75 * runtime_minutes * 60.0
            high = 1.35 * runtime_minutes * 60.0
            if not (low <= pl.duration <= high):
                continue
        candidates.append(pl)

    # Rule 4: identical clip sets are the same episode, duplicated once per
    # playback path. Keep the longest, drop the rest.
    groups = {}
    for pl in candidates:
        groups.setdefault(_clip_key(pl), []).append(pl)

    kept = []
    dropped = 0
    for key, group in groups.items():
        group.sort(key=lambda pl: (-pl.duration, pl.name))
        kept.append(group[0])
        for dup in group[1:]:
            dropped += 1
            warnings.append(
                "duplicate playlist %s dropped: same clip set as %s"
                % (dup.name, group[0].name)
            )
    if dropped:
        warnings.append(
            "%d candidate playlist(s) collapsed to %d episode(s) after clip-set dedup"
            % (len(candidates), len(kept))
        )

    # Rule 5: order by clip name. Sorting by duration is provably wrong.
    kept.sort(key=_order_key)

    for position, pl in enumerate(kept, start=1):
        plan.episodes.append(
            Episode(
                index=position,
                start_number=0,
                clips=list(pl.clips),
                playlist=pl.name,
                duration=pl.duration,
            )
        )

    if not plan.episodes:
        warnings.append(
            "no episode found within %gs..%gs"
            % (min_episode_seconds, max_episode_seconds)
        )

    # Rule 7: bonus = playlists that are not an episode, are long enough to be
    # real content, and do not reuse an episode's clip. Clips referenced only by
    # multi-playitem playlists are NOT bonus: on the validation set that would
    # add 150 six-second menu clips to DISC1.
    episode_keys = set(groups)
    episode_clips = set()
    for pl in kept:
        episode_clips.update(pl.clips)

    bonus_groups = {}
    for pl in playlists:
        if not pl.clips:
            continue
        if _clip_key(pl) in episode_keys:
            continue
        if pl.duration < min_bonus_seconds:
            continue
        if pl.duration > max_episode_seconds:
            continue
        if set(pl.clips) & episode_clips:
            continue
        bonus_groups.setdefault(_clip_key(pl), []).append(pl)

    bonus_kept = []
    for group in bonus_groups.values():
        group.sort(key=lambda pl: (-pl.duration, pl.name))
        bonus_kept.append(group[0])
    bonus_kept.sort(key=_order_key)

    for pl in bonus_kept:
        plan.bonus.append(
            BonusItem(clips=list(pl.clips), playlist=pl.name, duration=pl.duration)
        )

    # Rule 6: cross-check only. On the validation set this criterion alone was
    # right for 1 of 3 discs, so it may never change the result.
    if large_clip_count is not None and int(large_clip_count) != len(plan.episodes):
        warnings.append(
            "cross-check mismatch: %d clip(s) larger than 1 GiB but %d episode(s) detected"
            % (int(large_clip_count), len(plan.episodes))
        )

    return plan


def discover_sibling_discs(bluray_root):
    """All disc directories next to bluray_root, each holding a BDMV dir.

    Sorted by the disc number in the directory name, so DISC1 < DISC2 < DISC10.
    """
    root = Path(bluray_root)
    parent = root.parent
    discs = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        entries = [root]

    for entry in entries:
        try:
            if entry.is_dir() and (entry / "BDMV").is_dir():
                discs.append(entry)
        except OSError:
            continue

    if root not in discs:
        try:
            if (root / "BDMV").is_dir():
                discs.append(root)
        except OSError:
            pass

    def sort_key(path):
        match = _DISC_NUMBER_RE.search(path.name)
        if match:
            return (0, int(match.group(1)), path.name.lower())
        match = _TRAILING_NUMBER_RE.search(path.name)
        if match:
            return (1, int(match.group(1)), path.name.lower())
        return (2, 0, path.name.lower())

    discs.sort(key=sort_key)
    return discs


def assign_episode_numbers(plans, *, offset=0):
    """Number episodes continuously across discs, in the given disc order.

    Mutates start_number on each episode in place and returns plans.
    """
    counter = int(offset)
    for plan in plans:
        for episode in plan.episodes:
            counter += 1
            episode.start_number = counter
    return list(plans)
