"""Blu-ray MPLS playlist parsing (pure Python standard library).

The MPLS layout used here was verified byte-by-byte against real discs:

    data[0:4]                      magic, e.g. b"MPLS"
    int.from_bytes(data[8:12])     playlist_start (absolute offset, 58 on the
                                   verified discs)
    pos = playlist_start + 6       playitem section
    int.from_bytes(data[pos:pos+2])  playitem_count; then pos += 4
    each playitem, starting at pos:
        +0..1    item_length (does NOT include these 2 bytes)
        +2..6    clip name, 5 ASCII bytes ("00334")
        +14..17  in_time   (45 kHz ticks)
        +18..21  out_time  (45 kHz ticks)
    pos += 2 + item_length         <-- advancing by item_length alone desyncs
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = [
    "TIME_BASE",
    "MplsError",
    "Playitem",
    "PlaylistInfo",
    "parse_mpls",
    "scan_playlists",
    "scan_playlists_with_errors",
    "find_bluray_root",
    "read_disc_meta",
]

TIME_BASE = 45000.0

_MPLS_MAX_SIZE = 1024 * 1024
_MPLS_MAX_PLAYITEMS = 200
_MPLS_MAX_DURATION_SECONDS = 12 * 60 * 60

_MPLS_MAGIC = b"MPLS"
_PLAYLIST_START_OFFSET = 8
_PLAYITEM_HEADER_BYTES = 22
_PLAYITEM_COUNT_SKIP = 4

_DISC_RE = re.compile(r"disc\s*(\d+)", re.IGNORECASE)
_SEASON_RE = re.compile(r"season\s*(\d+)", re.IGNORECASE)
_DIR_DISC_RE = re.compile(r"(?:disc|disk|dvd|cd)[\s._-]*0*(\d{1,3})", re.IGNORECASE)
_XML_NAME_RE = re.compile(r"<(?:[\w.-]+:)?name>\s*([^<]+?)\s*</(?:[\w.-]+:)?name>")
_XML_SETNUMBER_RE = re.compile(
    r"<(?:[\w.-]+:)?setNumber>\s*([^<]+?)\s*</(?:[\w.-]+:)?setNumber>"
)


class MplsError(Exception):
    """Raised when a file is not a usable MPLS playlist."""


class Playitem:
    """One playback item inside a playlist."""

    __slots__ = ("clip", "in_time", "out_time")

    def __init__(self, clip: str, in_time: int, out_time: int) -> None:
        self.clip = clip
        self.in_time = in_time
        self.out_time = out_time

    def __repr__(self) -> str:
        return "Playitem(clip=%r, in_time=%d, out_time=%d)" % (
            self.clip,
            self.in_time,
            self.out_time,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Playitem):
            return NotImplemented
        return (self.clip, self.in_time, self.out_time) == (
            other.clip,
            other.in_time,
            other.out_time,
        )

    def __hash__(self) -> int:
        return hash((self.clip, self.in_time, self.out_time))


class PlaylistInfo:
    """Parsed view of a single .mpls file.

    warnings is not part of the shared interface contract; it is populated only
    for tolerated anomalies (e.g. duration above _MPLS_MAX_DURATION_SECONDS) so
    callers can surface them without losing the playlist.
    """

    __slots__ = ("name", "path", "playitems", "duration", "clips", "warnings")

    def __init__(
        self,
        name: str,
        path: Path,
        playitems: List[Playitem],
        duration: float,
        clips: List[str],
        warnings: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.path = path
        self.playitems = playitems
        self.duration = duration
        self.clips = clips
        self.warnings = warnings if warnings is not None else []

    @property
    def playitem_count(self) -> int:
        return len(self.playitems)

    @property
    def is_single_clip(self) -> bool:
        return len(self.clips) == 1

    def __repr__(self) -> str:
        return "PlaylistInfo(name=%r, playitems=%d, duration=%.3f, clips=%r)" % (
            self.name,
            len(self.playitems),
            self.duration,
            self.clips,
        )


def _ordered_unique(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def parse_mpls(path) -> PlaylistInfo:
    """Parse one .mpls file into a PlaylistInfo. Raises MplsError on failure."""
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise MplsError("cannot stat %s: %s" % (file_path, exc)) from exc
    if size > _MPLS_MAX_SIZE:
        raise MplsError(
            "%s is %d bytes, above the %d byte limit" % (file_path, size, _MPLS_MAX_SIZE)
        )
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise MplsError("cannot read %s: %s" % (file_path, exc)) from exc

    if data[: len(_MPLS_MAGIC)] != _MPLS_MAGIC:
        raise MplsError("%s is not an MPLS playlist (magic %r)" % (file_path, data[:8]))

    if len(data) < _PLAYLIST_START_OFFSET + 4:
        raise MplsError("%s is truncated (%d bytes)" % (file_path, len(data)))
    playlist_start = int.from_bytes(data[_PLAYLIST_START_OFFSET:_PLAYLIST_START_OFFSET + 4], "big")

    pos = playlist_start + 6
    if pos + 2 > len(data):
        raise MplsError(
            "%s: playlist_start %d is outside the file (offset %d, size %d)"
            % (file_path, playlist_start, pos, len(data))
        )
    playitem_count = int.from_bytes(data[pos:pos + 2], "big")
    pos += _PLAYITEM_COUNT_SKIP

    if playitem_count > _MPLS_MAX_PLAYITEMS:
        raise MplsError(
            "%s declares %d playitems, above the %d limit"
            % (file_path, playitem_count, _MPLS_MAX_PLAYITEMS)
        )

    playitems: List[Playitem] = []
    for index in range(playitem_count):
        if pos + _PLAYITEM_HEADER_BYTES > len(data):
            raise MplsError(
                "%s: playitem %d at offset %d runs past end of file (%d bytes)"
                % (file_path, index, pos, len(data))
            )
        item_length = int.from_bytes(data[pos:pos + 2], "big")
        clip = data[pos + 2:pos + 7].decode("ascii", "replace")
        in_time = int.from_bytes(data[pos + 14:pos + 18], "big")
        out_time = int.from_bytes(data[pos + 18:pos + 22], "big")
        playitems.append(Playitem(clip, in_time, out_time))
        pos += 2 + item_length

    duration = sum(item.out_time - item.in_time for item in playitems) / TIME_BASE
    warnings: List[str] = []
    if duration > _MPLS_MAX_DURATION_SECONDS:
        warnings.append(
            "%s duration %.1fs exceeds %.1fs"
            % (file_path.name, duration, _MPLS_MAX_DURATION_SECONDS)
        )

    return PlaylistInfo(
        name=file_path.name,
        path=file_path,
        playitems=playitems,
        duration=duration,
        clips=_ordered_unique([item.clip for item in playitems]),
        warnings=warnings,
    )


def _playlist_dir(bdmv_dir: Path) -> Optional[Path]:
    candidate = _find_child_ci(bdmv_dir, "PLAYLIST")
    if candidate is not None and candidate.is_dir():
        return candidate
    nested = _find_child_ci(bdmv_dir, "BDMV")
    if nested is not None:
        candidate = _find_child_ci(nested, "PLAYLIST")
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


def scan_playlists(bdmv_dir, errors: Optional[List[str]] = None) -> List[PlaylistInfo]:
    """Scan BDMV/PLAYLIST/*.mpls and return parsed playlists sorted by name.

    bdmv_dir is the BDMV directory (a parent that contains BDMV is accepted too).
    A file that fails to parse never aborts the scan: it is skipped, and when a
    mutable list is passed as errors, one message per skipped file is appended to
    it. Returns an empty list when the directory does not exist.
    """
    playlists, _ = _scan(Path(bdmv_dir), errors)
    return playlists


def scan_playlists_with_errors(bdmv_dir) -> Tuple[List[PlaylistInfo], List[str]]:
    """Same as scan_playlists but returns (playlists, errors)."""
    return _scan(Path(bdmv_dir), None)


def _scan(bdmv_dir: Path, errors: Optional[List[str]]):
    collected: List[str] = []
    playlists: List[PlaylistInfo] = []
    if not bdmv_dir.is_dir():
        collected.append("not a directory: %s" % bdmv_dir)
    else:
        playlist_dir = _playlist_dir(bdmv_dir)
        if playlist_dir is None:
            collected.append("no PLAYLIST directory under %s" % bdmv_dir)
        else:
            files = sorted(
                (p for p in playlist_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mpls"),
                key=lambda p: p.name.lower(),
            )
            for file_path in files:
                try:
                    playlists.append(parse_mpls(file_path))
                except MplsError as exc:
                    collected.append(str(exc))
                except Exception as exc:  # never let one file abort the scan
                    collected.append("%s: %s: %s" % (file_path, type(exc).__name__, exc))
    if errors is not None:
        errors.extend(collected)
    return playlists, collected


def _find_child_ci(directory: Path, name: str) -> Optional[Path]:
    """Case-insensitive child lookup that works on Windows, SMB and Linux."""
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


def find_bluray_root(path) -> Optional[Path]:
    """Normalize any path inside a disc to the directory that contains BDMV.

    Accepted inputs, case-insensitively: a directory containing BDMV; the BDMV
    directory itself; a path below BDMV (e.g. BDMV/STREAM/00001.m2ts). A
    component literally named BDMV is honoured even when the path does not
    exist on disk, so unverified caller input still normalizes. Returns None
    when no BDMV can be located by name or on disk.
    """
    try:
        current = Path(path)
    except (TypeError, ValueError):
        return None
    if not str(current):
        return None
    if not current.is_dir():
        current = current.parent

    for candidate in [current] + list(current.parents):
        if candidate.name.lower() == "bdmv":
            return candidate.parent
        bdmv = _find_child_ci(candidate, "BDMV")
        if bdmv is not None and bdmv.is_dir():
            return candidate
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_from_xml(file_path: Path):
    """Return (name, set_number) from one bdmt_*.xml, or (None, None)."""
    try:
        root = ET.parse(str(file_path)).getroot()
    except Exception:
        return None, None
    name: Optional[str] = None
    set_number: Optional[str] = None
    for element in root.iter():
        local = _local_name(element.tag)
        text = (element.text or "").strip()
        if not text:
            continue
        if local == "name" and name is None:
            name = text
        elif local == "setNumber" and set_number is None:
            set_number = text
    return name, set_number


def _extract_from_raw_text(file_path: Path):
    """Regex fallback for bdmt_*.xml files that ElementTree refuses to parse."""
    try:
        raw = file_path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None, None
    name_match = _XML_NAME_RE.search(raw)
    set_match = _XML_SETNUMBER_RE.search(raw)
    return (
        name_match.group(1).strip() if name_match else None,
        set_match.group(1).strip() if set_match else None,
    )


def read_disc_meta(bluray_root) -> dict:
    """Read disc metadata from BDMV/META/DL/bdmt_*.xml, degrading gracefully.

    Returns {"disc_number": Optional[int], "season": Optional[int],
             "title": Optional[str], "source": str}.

    source is "bdmt" when the numbers came from a metadata XML, "dirname" when
    only the disc number could be recovered from the directory name, and "none"
    when nothing could be read. This function never raises.
    """
    meta: Dict[str, object] = {
        "disc_number": None,
        "season": None,
        "title": None,
        "source": "none",
    }
    try:
        root = find_bluray_root(bluray_root)
        if root is None:
            root = Path(bluray_root)
        disc_number: Optional[int] = None
        season: Optional[int] = None
        title: Optional[str] = None
        found_bdmt = False

        meta_dir = _find_child_ci(root, "BDMV")
        if meta_dir is not None:
            meta_dir = _find_child_ci(meta_dir, "META")
        if meta_dir is not None:
            meta_dir = _find_child_ci(meta_dir, "DL")
        if meta_dir is not None and meta_dir.is_dir():
            try:
                xml_files = sorted(
                    (p for p in meta_dir.iterdir()
                     if p.is_file() and p.name.lower().startswith("bdmt_")
                     and p.suffix.lower() == ".xml"),
                    key=lambda p: p.name.lower(),
                )
            except OSError:
                xml_files = []
            for xml_file in xml_files:
                name, set_number = _extract_from_xml(xml_file)
                if name is None and set_number is None:
                    name, set_number = _extract_from_raw_text(xml_file)
                if name is None and set_number is None:
                    continue
                found_bdmt = True
                if title is None and name:
                    title = name
                if name:
                    if disc_number is None:
                        match = _DISC_RE.search(name)
                        if match:
                            disc_number = int(match.group(1))
                    if season is None:
                        match = _SEASON_RE.search(name)
                        if match:
                            season = int(match.group(1))
                if disc_number is None and set_number:
                    try:
                        disc_number = int(set_number.strip())
                    except ValueError:
                        pass
                if disc_number is not None and season is not None:
                    break

        if disc_number is None:
            match = _DIR_DISC_RE.search(root.name)
            if match:
                disc_number = int(match.group(1))

        if disc_number is not None or season is not None:
            meta["source"] = "bdmt" if found_bdmt else "dirname"
        meta["disc_number"] = disc_number
        meta["season"] = season
        meta["title"] = title
    except Exception:
        pass
    return meta
