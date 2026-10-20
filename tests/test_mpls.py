"""Real-disc tests for plugins/blurayepisodesplit/mpls.py.

Run with:
    python tests/test_mpls.py

Requires the Preacher S01 Blu-ray share to be reachable; if it is not, the
real-disc tests are reported as SKIP and the synthetic tests still run.
"""

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "blurayepisodesplit"))

import mpls  # noqa: E402
from mpls import (  # noqa: E402
    TIME_BASE,
    MplsError,
    Playitem,
    PlaylistInfo,
    find_bluray_root,
    parse_mpls,
    read_disc_meta,
    scan_playlists,
    scan_playlists_with_errors,
)

BASE = Path(
    r"\\DS1819\video\源数据\电视剧\欧美剧"
    r"\Preacher.S01.2016.1080p.BluRay.AVC.DTS-HD.MA.5.1-DIY@Audies"
)
DISCS = {"DISC1": BASE / "DISC1", "DISC2": BASE / "DISC2", "DISC3": BASE / "DISC3"}

_passed = 0
_failed = []
_skipped = []


def check(name, fn):
    global _passed
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - report every failure, keep going
        _failed.append((name, exc, traceback.format_exc()))
        print("FAIL %s -> %s: %s" % (name, type(exc).__name__, exc))
    else:
        _passed += 1
        print("PASS %s" % name)


def skip(name, reason):
    _skipped.append(name)
    print("SKIP %s -> %s" % (name, reason))


def eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError("%s expected %r, got %r" % (what or "value", expected, actual))


def approx(actual, expected, tol=0.1, what=""):
    if abs(actual - expected) > tol:
        raise AssertionError(
            "%s expected %.4f +/- %.4f, got %.4f" % (what or "value", expected, tol, actual)
        )


def assert_raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("expected %s, got %s: %s" % (exc_type.__name__, type(exc).__name__, exc))
    raise AssertionError("expected %s, nothing raised" % exc_type.__name__)


# ---------------------------------------------------------------- constants --
def test_time_base():
    eq(TIME_BASE, 45000.0, "TIME_BASE")


def test_guards():
    eq(mpls._MPLS_MAX_SIZE, 1024 * 1024, "_MPLS_MAX_SIZE")
    eq(mpls._MPLS_MAX_PLAYITEMS, 200, "_MPLS_MAX_PLAYITEMS")
    eq(mpls._MPLS_MAX_DURATION_SECONDS, 12 * 60 * 60, "_MPLS_MAX_DURATION_SECONDS")


def test_api_surface():
    for name in (
        "parse_mpls",
        "scan_playlists",
        "scan_playlists_with_errors",
        "find_bluray_root",
        "read_disc_meta",
    ):
        if not callable(getattr(mpls, name, None)):
            raise AssertionError("missing callable %s" % name)
    info = PlaylistInfo("x.mpls", Path("x.mpls"), [Playitem("00001", 0, TIME_BASE.__int__())], 1.0, ["00001"])
    eq(info.playitem_count, 1, "playitem_count")
    eq(info.is_single_clip, True, "is_single_clip")
    two = PlaylistInfo("y.mpls", Path("y.mpls"), [], 0.0, ["00001", "00002"])
    eq(two.is_single_clip, False, "is_single_clip with two clips")


# ------------------------------------------------------------ synthetic I/O --
def _build_mpls(playitems, playlist_start=58, magic=b"MPLS0200"):
    """Build a minimal MPLS blob matching the verified byte layout."""
    header = bytearray(playlist_start)
    header[0:len(magic)] = magic
    header[8:12] = playlist_start.to_bytes(4, "big")
    body = bytearray()
    body += b"\x00" * 6
    body += len(playitems).to_bytes(2, "big")
    body += b"\x00\x00"
    for clip, in_time, out_time in playitems:
        # item_length counts the payload only; the 2 length bytes sit in front,
        # so the whole item occupies 2 + item_length bytes.
        item = bytearray(2 + 64)
        item[0:2] = (64).to_bytes(2, "big")
        item[2:7] = clip.encode("ascii")
        item[14:18] = in_time.to_bytes(4, "big")
        item[18:22] = out_time.to_bytes(4, "big")
        body += item
    return bytes(header) + bytes(body)


def test_synthetic_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        target = tmp_path / "00001.mpls"
        target.write_bytes(_build_mpls([("00334", 524280, 175614195), ("00335", 0, 45000)]))
        info = parse_mpls(target)
        eq([(i.clip, i.in_time, i.out_time) for i in info.playitems],
           [("00334", 524280, 175614195), ("00335", 0, 45000)], "playitems")
        eq(info.clips, ["00334", "00335"], "clips")
        eq(info.is_single_clip, False, "is_single_clip")
        eq(info.name, "00001.mpls", "name")
        approx(info.duration, (175614195 - 524280 + 45000) / 45000.0, 1e-9, "duration")


def test_synthetic_bad_magic():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "bad.mpls"
        target.write_bytes(b"XXXX0200" + b"\x00" * 100)
        assert_raises(MplsError, lambda: parse_mpls(target))


def test_synthetic_missing_file():
    assert_raises(MplsError, lambda: parse_mpls(Path(tempfile.gettempdir()) / "__nope__.mpls"))


def test_synthetic_oversize():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "big.mpls"
        target.write_bytes(b"MPLS0200" + b"\x00" * (mpls._MPLS_MAX_SIZE + 1))
        assert_raises(MplsError, lambda: parse_mpls(target))


def test_synthetic_oob_offset_message():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "oob.mpls"
        blob = bytearray(_build_mpls([("00334", 0, 45000)]))
        # claim 50 playitems while only one is present -> must raise, message carries offset
        blob[58 + 6:58 + 8] = (50).to_bytes(2, "big")
        target.write_bytes(bytes(blob))
        try:
            parse_mpls(target)
        except MplsError as exc:
            if "offset" not in str(exc):
                raise AssertionError("error message lacks offset: %s" % exc)
        else:
            raise AssertionError("expected MplsError for truncated playitems")


def test_synthetic_too_many_playitems():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "many.mpls"
        blob = bytearray(_build_mpls([("00334", 0, 45000)]))
        blob[58 + 6:58 + 8] = (201).to_bytes(2, "big")
        target.write_bytes(bytes(blob))
        assert_raises(MplsError, lambda: parse_mpls(target))


def test_synthetic_long_duration_warns_but_returns():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "long.mpls"
        # 13 hours: above the 12 hour cap
        ticks = int(13 * 3600 * TIME_BASE)
        target.write_bytes(_build_mpls([("00334", 0, ticks)]))
        info = parse_mpls(target)
        approx(info.duration, 13 * 3600, 0.5, "duration")
        if not info.warnings:
            raise AssertionError("expected a warning for over-long playlist")


def test_synthetic_scan_skips_broken():
    with tempfile.TemporaryDirectory() as tmp:
        bdmv = Path(tmp) / "BDMV"
        playlist_dir = bdmv / "PLAYLIST"
        playlist_dir.mkdir(parents=True)
        (playlist_dir / "00001.mpls").write_bytes(_build_mpls([("00334", 0, 45000)]))
        (playlist_dir / "00002.mpls").write_bytes(b"NOPE" + b"\x00" * 60)
        (playlist_dir / "00003.mpls").write_bytes(_build_mpls([("00335", 0, 90000)]))
        errors = []
        found = scan_playlists(bdmv, errors=errors)
        eq(len(found), 2, "playlist count")
        eq(len(errors), 1, "error count")
        found2, errors2 = scan_playlists_with_errors(bdmv)
        eq(len(found2), 2, "playlist count via with_errors")
        eq(len(errors2), 1, "error count via with_errors")


def test_synthetic_scan_missing_dir():
    errors = []
    found = scan_playlists(Path(tempfile.gettempdir()) / "__no_bdmv_here__", errors=errors)
    eq(found, [], "empty result")
    if not errors:
        raise AssertionError("expected an error entry for a missing directory")


def test_find_root_synthetic():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "DISC1"
        (root / "BDMV" / "STREAM").mkdir(parents=True)
        eq(find_bluray_root(root), root, "root itself")
        eq(find_bluray_root(root / "BDMV"), root, "BDMV")
        eq(find_bluray_root(root / "BDMV" / "STREAM" / "00001.m2ts"), root, "m2ts")
    eq(find_bluray_root(Path(tempfile.gettempdir())), None, "no BDMV -> None")


# --------------------------------------------------------------- real discs --
HAVE_DISC = {name: (path / "BDMV" / "PLAYLIST").is_dir() for name, path in DISCS.items()}


def _require(disc):
    if not HAVE_DISC[disc]:
        raise _SkipDisc(disc)


class _SkipDisc(Exception):
    pass


def test_real_disc1_00334():
    _require("DISC1")
    info = parse_mpls(DISCS["DISC1"] / "BDMV" / "PLAYLIST" / "00334.mpls")
    eq([(i.clip, i.in_time, i.out_time) for i in info.playitems],
       [("00334", 524280, 175614195)], "playitems")
    approx(info.duration, 3890.9, 0.1, "duration")
    eq(info.playitem_count, 1, "playitem_count")
    eq(info.is_single_clip, True, "is_single_clip")
    eq(info.clips, ["00334"], "clips")
    eq(info.name, "00334.mpls", "name")


def test_real_disc1_00096():
    _require("DISC1")
    info = parse_mpls(DISCS["DISC1"] / "BDMV" / "PLAYLIST" / "00096.mpls")
    eq(info.playitem_count, 101, "playitem_count")


def test_real_disc1_00099():
    _require("DISC1")
    info = parse_mpls(DISCS["DISC1"] / "BDMV" / "PLAYLIST" / "00099.mpls")
    eq(info.playitem_count, 100, "playitem_count")


def _scan_check(disc, expected):
    _require(disc)
    errors = []
    found = scan_playlists(DISCS[disc] / "BDMV", errors=errors)
    eq(len(found), expected, "%s playlist count" % disc)
    eq(len(errors), 0, "%s error count" % disc)
    found2, errors2 = scan_playlists_with_errors(DISCS[disc] / "BDMV")
    eq(len(found2), expected, "%s playlist count (with_errors)" % disc)
    eq(len(errors2), 0, "%s error count (with_errors)" % disc)


def test_real_scan_disc1():
    _scan_check("DISC1", 204)


def test_real_scan_disc2():
    _scan_check("DISC2", 190)


def test_real_scan_disc3():
    _scan_check("DISC3", 176)


def test_real_episode_durations():
    expected = {
        "DISC1": {"00334": 3890.9, "00335": 2951.9, "00336": 2526.5, "00337": 2561.6},
        # DISC2's 4th episode is the exception: the clip is 00348 but the
        # playlist carrying it is 00362.mpls (00348.mpls is a 20s extra for
        # clip 00340), so playlist name != clip name there.
        "DISC2": {"00335": 2535.5, "00336": 2641.6, "00337": 2531.5, "00362": 2576.6},
        "DISC3": {"00334": 2861.8, "00335": 3269.7},
    }
    for disc, items in expected.items():
        _require(disc)
        for clip, seconds in items.items():
            info = parse_mpls(DISCS[disc] / "BDMV" / "PLAYLIST" / ("%s.mpls" % clip))
            approx(info.duration, seconds, 0.1, "%s/%s duration" % (disc, clip))
            eq(info.is_single_clip, True, "%s/%s single clip" % (disc, clip))
            expected_clip = "00348" if clip == "00362" else clip
            eq(info.clips, [expected_clip], "%s/%s clips" % (disc, clip))


def test_real_find_bluray_root():
    _require("DISC1")
    root = DISCS["DISC1"]
    eq(find_bluray_root(root), root, "disc dir")
    eq(find_bluray_root(root / "BDMV"), root, "BDMV")
    eq(find_bluray_root(root / "BDMV" / "STREAM" / "00334.m2ts"), root, "m2ts")


def test_real_read_disc_meta():
    _require("DISC1")
    meta = read_disc_meta(DISCS["DISC1"])
    eq(meta["disc_number"], 1, "disc_number")
    eq(meta["season"], 1, "season")
    if not isinstance(meta["title"], str) or "Preacher" not in meta["title"]:
        raise AssertionError("unexpected title: %r" % (meta["title"],))
    eq(meta["source"], "bdmt", "source")


def test_real_read_disc_meta_all_discs():
    for expected_number, disc in ((1, "DISC1"), (2, "DISC2"), (3, "DISC3")):
        _require(disc)
        meta = read_disc_meta(DISCS[disc])
        eq(meta["disc_number"], expected_number, "%s disc_number" % disc)
        eq(meta["season"], 1, "%s season" % disc)


def test_real_read_disc_meta_never_raises():
    eq(read_disc_meta(Path(tempfile.gettempdir()) / "__nope__")["disc_number"], None, "missing dir")
    eq(read_disc_meta(Path(tempfile.gettempdir()) / "__nope__")["season"], None, "missing dir season")
    eq(read_disc_meta(Path(tempfile.gettempdir()) / "__nope__")["source"], "none", "missing dir source")


TESTS = [
    ("constants/TIME_BASE", test_time_base),
    ("constants/guards", test_guards),
    ("api/surface", test_api_surface),
    ("synthetic/roundtrip", test_synthetic_roundtrip),
    ("synthetic/bad-magic", test_synthetic_bad_magic),
    ("synthetic/missing-file", test_synthetic_missing_file),
    ("synthetic/oversize", test_synthetic_oversize),
    ("synthetic/oob-offset-message", test_synthetic_oob_offset_message),
    ("synthetic/too-many-playitems", test_synthetic_too_many_playitems),
    ("synthetic/long-duration-warns", test_synthetic_long_duration_warns_but_returns),
    ("synthetic/scan-skips-broken", test_synthetic_scan_skips_broken),
    ("synthetic/scan-missing-dir", test_synthetic_scan_missing_dir),
    ("synthetic/find-root", test_find_root_synthetic),
    ("real/DISC1-00334", test_real_disc1_00334),
    ("real/DISC1-00096-count", test_real_disc1_00096),
    ("real/DISC1-00099-count", test_real_disc1_00099),
    ("real/scan-DISC1", test_real_scan_disc1),
    ("real/scan-DISC2", test_real_scan_disc2),
    ("real/scan-DISC3", test_real_scan_disc3),
    ("real/episode-durations", test_real_episode_durations),
    ("real/find-bluray-root", test_real_find_bluray_root),
    ("real/read-disc-meta-DISC1", test_real_read_disc_meta),
    ("real/read-disc-meta-all", test_real_read_disc_meta_all_discs),
    ("real/read-disc-meta-never-raises", test_real_read_disc_meta_never_raises),
]


def main():
    for name, fn in TESTS:
        try:
            fn()
        except _SkipDisc as skip_exc:
            skip(name, "disc %s not reachable" % skip_exc)
        except Exception as exc:  # noqa: BLE001
            _failed.append((name, exc, traceback.format_exc()))
            print("FAIL %s -> %s: %s" % (name, type(exc).__name__, exc))
        else:
            globals()["_passed"] += 1
            print("PASS %s" % name)

    print("")
    print("passed=%d failed=%d skipped=%d total=%d"
          % (_passed, len(_failed), len(_skipped), len(TESTS)))
    if _failed:
        print("")
        for name, exc, tb in _failed:
            print("---- %s ----" % name)
            print(tb)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
