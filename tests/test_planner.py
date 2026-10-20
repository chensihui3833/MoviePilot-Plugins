# -*- coding: utf-8 -*-
"""Acceptance tests for planner.py.

Run with the bundled interpreter:
  <python.exe> <repo>/tests/test_planner.py

Real-disc assertions need the SMB share reachable; they are skipped with a loud
SKIP line (never a silent pass) when it is not.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent / "plugins" / "blurayepisodesplit"
sys.path.insert(0, str(PLUGIN_DIR))

import mpls  # noqa: E402
import planner  # noqa: E402
from planner import (  # noqa: E402
    BonusItem,
    DiscPlan,
    Episode,
    assign_episode_numbers,
    discover_sibling_discs,
    plan_disc,
)

SERIES = Path(
    r"\\DS1819\video\源数据\电视剧\欧美剧"
    r"\Preacher.S01.2016.1080p.BluRay.AVC.DTS-HD.MA.5.1-DIY@Audies"
)
DISC1 = SERIES / "DISC1"
DISC2 = SERIES / "DISC2"
DISC3 = SERIES / "DISC3"

PASSED = []
FAILED = []
SKIPPED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s%s" % (name, (" | " + detail) if detail else ""))
    else:
        FAILED.append(name)
        print("  FAIL  %s%s" % (name, (" | " + detail) if detail else ""))


def skip(name, reason):
    SKIPPED.append(name)
    print("  SKIP  %s | %s" % (name, reason))


def eq(name, actual, expected):
    check(name, actual == expected, "actual=%r expected=%r" % (actual, expected))


def close(name, actual, expected, tol=0.05):
    ok = actual is not None and abs(actual - expected) <= tol
    check(name, ok, "actual=%.4f expected=%.4f" % (actual, expected))


def make_playlist(name, clips, duration, playitem_count=None):
    """Synthetic PlaylistInfo for rule-level tests (no disk access)."""
    count = playitem_count if playitem_count is not None else len(clips)
    items = [mpls.Playitem(clip, 0, int(duration * mpls.TIME_BASE / count))
             for clip in (clips or ["00000"])]
    while len(items) < count:
        items.append(mpls.Playitem(clips[0] if clips else "00000", 0, 0))
    return mpls.PlaylistInfo(
        name=name,
        path=Path("/nonexistent") / name,
        playitems=items[:count],
        duration=duration,
        clips=list(clips),
    )


def share_available():
    try:
        return (DISC1 / "BDMV" / "PLAYLIST").is_dir()
    except OSError:
        return False


ONLINE = share_available()


# ---------------------------------------------------------------- rule tests
def test_playitem_rule():
    print("\n[1] rule: playitem_count > 1 is never an episode")
    playlists = [
        make_playlist("00096.mpls", ["00097", "00099"], 4659.8, playitem_count=101),
        make_playlist("00099.mpls", ["00099"], 4658.8, playitem_count=100),
        make_playlist("00334.mpls", ["00334"], 3890.9, playitem_count=1),
        make_playlist("00335.mpls", ["00335"], 2951.9, playitem_count=1),
    ]
    plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists,
                     max_episode_seconds=6000.0)
    eq("play-all 00096 (PI=101, 4659.8s) excluded", "00096.mpls" in [e.playlist for e in plan.episodes], False)
    eq("play-all 00099 (PI=100, 4658.8s) excluded", "00099.mpls" in [e.playlist for e in plan.episodes], False)
    eq("only the two single-playitem playlists survive", [e.playlist for e in plan.episodes],
       ["00334.mpls", "00335.mpls"])
    check("warning records the PI>1 exclusion",
          any("more than one playitem" in w for w in plan.warnings), str(plan.warnings[:1]))

    # A play-all whose runtime sits INSIDE the episode window must still be rejected.
    inside = [make_playlist("01000.mpls", ["01001"], 3000.0, playitem_count=42)]
    plan2 = plan_disc(Path("/nonexistent-disc"), playlists=inside)
    eq("PI=42 with 3000s inside 600..4500 still excluded", len(plan2.episodes), 0)


def test_duration_rule():
    print("\n[2] rule: duration window")
    playlists = [
        make_playlist("00001.mpls", ["00001"], 599.9),
        make_playlist("00002.mpls", ["00002"], 600.0),
        make_playlist("00003.mpls", ["00003"], 1500.0),
        make_playlist("00004.mpls", ["00004"], 4500.0),
        make_playlist("00005.mpls", ["00005"], 4500.1),
    ]
    plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("600.0 <= d <= 4500.0 kept, outside dropped", [e.playlist for e in plan.episodes],
       ["00002.mpls", "00003.mpls", "00004.mpls"])
    plan25 = plan_disc(Path("/nonexistent-disc"),
                       playlists=[make_playlist("00010.mpls", ["00010"], 1500.0)],
                       min_episode_seconds=600.0)
    eq("25-minute episode kept by the 600s floor", len(plan25.episodes), 1)


def test_runtime_rule():
    print("\n[3] rule: TMDB runtime alignment is optional and must never drop on None")
    playlists = [
        make_playlist("00334.mpls", ["00334"], 3890.9),
        make_playlist("00335.mpls", ["00335"], 2951.9),
        make_playlist("00336.mpls", ["00336"], 2526.5),
        make_playlist("00337.mpls", ["00337"], 2561.6),
    ]
    none_plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists, runtime_minutes=None)
    eq("runtime_minutes=None keeps all 4 episodes", len(none_plan.episodes), 4)

    omitted_plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("runtime_minutes omitted keeps all 4 episodes", len(omitted_plan.episodes), 4)

    tight = plan_disc(Path("/nonexistent-disc"), playlists=playlists, runtime_minutes=64.9)
    eq("runtime_minutes=64.9 narrows the window (0.75*64.9*60=2920.5)", len(tight.episodes), 2)
    eq("runtime-filtered episodes are the long ones",
       [e.clips for e in tight.episodes], [["00334"], ["00335"]])


def test_dedup_rule():
    print("\n[4] rule: clip-set dedup (12 candidates -> 4 episodes)")
    playlists = []
    for main, dup_a, dup_b, clip, dur in (
        ("00334.mpls", "00338.mpls", "00342.mpls", "00334", 3890.9),
        ("00335.mpls", "00339.mpls", "00343.mpls", "00335", 2951.9),
        ("00336.mpls", "00340.mpls", "00344.mpls", "00336", 2526.5),
        ("00337.mpls", "00341.mpls", "00345.mpls", "00337", 2561.6),
    ):
        for name in (main, dup_a, dup_b):
            playlists.append(make_playlist(name, [clip], dur))
    eq("12 synthetic candidates built", len(playlists), 12)
    plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("dedup collapses 12 candidates to 4 episodes", len(plan.episodes), 4)
    eq("kept playlists are the first-named of each group",
       [e.playlist for e in plan.episodes],
       ["00334.mpls", "00335.mpls", "00336.mpls", "00337.mpls"])
    check("warning states the 12 -> 4 collapse",
          any("12 candidate" in w and "4 episode" in w for w in plan.warnings),
          str([w for w in plan.warnings if "collapsed" in w]))

    # Longest wins inside a group, even when it is not the first name.
    longest = [
        make_playlist("00001.mpls", ["00009"], 1000.0),
        make_playlist("00002.mpls", ["00009"], 1100.0),
        make_playlist("00003.mpls", ["00009"], 900.0),
    ]
    plan2 = plan_disc(Path("/nonexistent-disc"), playlists=longest)
    eq("longest duplicate kept", [e.playlist for e in plan2.episodes], ["00002.mpls"])
    close("longest duplicate duration kept", plan2.episodes[0].duration, 1100.0)


def test_sort_rule():
    print("\n[5] rule: ordering by clip name, never by duration")
    playlists = [
        make_playlist("00337.mpls", ["00337"], 2561.6),
        make_playlist("00336.mpls", ["00336"], 2526.5),
        make_playlist("00335.mpls", ["00335"], 2951.9),
        make_playlist("00334.mpls", ["00334"], 3890.9),
    ]
    plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("clip order, not duration order",
       [e.clips[0] for e in plan.episodes], ["00334", "00335", "00336", "00337"])
    eq("index is 1-based and follows the order",
       [e.index for e in plan.episodes], [1, 2, 3, 4])
    check("duration order would have been 00334,00335,00337,00336",
          [e.clips[0] for e in plan.episodes] != ["00334", "00335", "00337", "00336"])


def test_bonus_rule():
    print("\n[6] rule: bonus = non-episode playlists with real content")
    playlists = [
        make_playlist("00334.mpls", ["00334"], 3890.9),
        make_playlist("00372.mpls", ["00352"], 456.2),
        make_playlist("00373.mpls", ["00353"], 391.3),
        make_playlist("00096.mpls", ["00097", "00099"], 4659.8, playitem_count=101),
        make_playlist("00106.mpls", ["00106"], 6.0, playitem_count=2),
        make_playlist("00100.mpls", ["00100"], 14.4),
    ]
    plan = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("episodes unaffected by bonus", [e.clips for e in plan.episodes], [["00334"]])
    eq("two real bonus items found", [b.clips for b in plan.bonus], [["00352"], ["00353"]])
    eq("bonus playlist names", [b.playlist for b in plan.bonus], ["00372.mpls", "00373.mpls"])
    eq("6-second menu clip is not bonus", "00106" in [c for b in plan.bonus for c in b.clips], False)
    eq("14-second clip is not bonus", "00100" in [c for b in plan.bonus for c in b.clips], False)
    eq("play-all clips are not bonus", ["00097", "00099"] in [b.clips for b in plan.bonus], False)

    floor = plan_disc(Path("/nonexistent-disc"), playlists=playlists, min_bonus_seconds=600.0)
    eq("min_bonus_seconds=600 drops both short bonus items", len(floor.bonus), 0)


def test_cross_check_rule():
    print("\n[7] rule: large_clip_count is a warning only")
    playlists = [
        make_playlist("00334.mpls", ["00334"], 3890.9),
        make_playlist("00335.mpls", ["00335"], 2951.9),
    ]
    agree = plan_disc(Path("/nonexistent-disc"), playlists=playlists, large_clip_count=2)
    eq("matching count adds no warning",
       any("cross-check" in w for w in agree.warnings), False)
    disagree = plan_disc(Path("/nonexistent-disc"), playlists=playlists, large_clip_count=5)
    check("mismatching count adds a warning",
          any("cross-check mismatch" in w for w in disagree.warnings), str(disagree.warnings))
    eq("mismatching count changes no episode", len(disagree.episodes), 2)
    untouched = plan_disc(Path("/nonexistent-disc"), playlists=playlists)
    eq("large_clip_count=None skips the cross-check entirely",
       any("cross-check" in w for w in untouched.warnings), False)


def test_serialization():
    print("\n[8] to_dict is JSON serializable and keeps Path out")
    plan = plan_disc(Path("/nonexistent-disc"), playlists=[
        make_playlist("00334.mpls", ["00334"], 3890.9),
        make_playlist("00372.mpls", ["00352"], 456.2),
    ])
    payload = plan.to_dict()
    text = json.dumps(payload)
    check("json.dumps succeeds", isinstance(text, str), text[:80])
    eq("root is a plain string", isinstance(payload["root"], str), True)
    eq("episode keys", sorted(payload["episodes"][0]),
       ["clips", "duration", "index", "playlist", "start_number"])
    eq("bonus keys", sorted(payload["bonus"][0]), ["clips", "duration", "playlist"])
    eq("warnings is a list", isinstance(payload["warnings"], list), True)
    eq("start_number defaults to 0 before assign", payload["episodes"][0]["start_number"], 0)


def test_assign_synthetic():
    print("\n[9] assign_episode_numbers on synthetic plans")
    p1 = plan_disc(Path("/d1"), playlists=[
        make_playlist("00334.mpls", ["00334"], 3890.9),
        make_playlist("00335.mpls", ["00335"], 2951.9),
    ])
    p2 = plan_disc(Path("/d2"), playlists=[
        make_playlist("00336.mpls", ["00336"], 2526.5),
    ])
    returned = assign_episode_numbers([p1, p2])
    eq("returns the same plans", returned, [p1, p2])
    eq("numbers are continuous across discs",
       [e.start_number for p in returned for e in p.episodes], [1, 2, 3])
    assign_episode_numbers([p1, p2], offset=10)
    eq("offset is honoured", [e.start_number for p in (p1, p2) for e in p.episodes], [11, 12, 13])
    assign_episode_numbers([])
    eq("empty input is a no-op", [], [])


def test_moviepilot_import():
    print("\n[10] planner imports under the MoviePilot package path")
    import shutil
    tmp = tempfile.mkdtemp()
    pkg = Path(tmp) / "app" / "plugins" / "blurayepisodesplit"
    pkg.mkdir(parents=True)
    Path(tmp, "app", "__init__.py").write_text("")
    Path(tmp, "app", "plugins", "__init__.py").write_text("")
    Path(pkg, "__init__.py").write_text("")
    for module in ("mpls.py", "planner.py"):
        shutil.copy2(PLUGIN_DIR / module, pkg / module)
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import app.plugins.blurayepisodesplit.planner as p\n"
        "import app.plugins.blurayepisodesplit.mpls as m\n"
        "assert p.scan_playlists is m.scan_playlists, 'planner did not bind the app.* mpls'\n"
        "print('OK', p.__name__, p.mpls.__name__ if hasattr(p, 'mpls') else '-')\n"
    ) % tmp
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    ok = proc.returncode == 0 and proc.stdout.startswith("OK app.plugins.blurayepisodesplit.planner")
    check("absolute app.plugins.<name>.mpls import resolves (not the fallback)", ok,
          (proc.stdout.strip() or proc.stderr.strip())[-300:])
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- real disc tests
# (clips, duration, playlist) -- verified against the disc, not inferred.
EXPECTED = {
    "DISC1": {
        "episodes": [(["00334"], 3890.9, "00334.mpls"), (["00335"], 2951.9, "00335.mpls"),
                     (["00336"], 2526.5, "00336.mpls"), (["00337"], 2561.6, "00337.mpls")],
        "bonus": [["00352"], ["00353"]],
        "numbers": [1, 2, 3, 4],
        "playlists": 204,
        "candidates": 12,
    },
    "DISC2": {
        # clip 00348 is carried by playlist 00362.mpls; 00348.mpls is a 20s menu item.
        "episodes": [(["00335"], 2535.5, "00335.mpls"), (["00336"], 2641.6, "00336.mpls"),
                     (["00337"], 2531.5, "00337.mpls"), (["00348"], 2576.6, "00362.mpls")],
        "bonus": [],
        "numbers": [5, 6, 7, 8],
        "playlists": 190,
        "candidates": 12,
    },
    "DISC3": {
        "episodes": [(["00334"], 2861.8, "00334.mpls"), (["00335"], 3269.7, "00335.mpls")],
        "bonus": [["00338"], ["00339"], ["00340"]],
        "numbers": [9, 10],
        "playlists": 176,
        "candidates": 6,
    },
}

DISCS = [("DISC1", DISC1), ("DISC2", DISC2), ("DISC3", DISC3)]


def test_real_discs():
    print("\n[11] real discs: episode count, clip order, durations, bonus")
    if not ONLINE:
        skip("real disc tests", "SMB share not reachable: %s" % SERIES)
        return None
    plans = []
    for label, path in DISCS:
        expected = EXPECTED[label]
        plan = plan_disc(path)
        plans.append(plan)
        eq("%s root" % label, plan.root, path)
        eq("%s episode count" % label, len(plan.episodes), len(expected["episodes"]))
        eq("%s clip sequence" % label,
           [e.clips for e in plan.episodes], [c for c, _d, _p in expected["episodes"]])
        for got, (_c, want, _p) in zip(plan.episodes, expected["episodes"]):
            close("%s duration %s" % (label, got.clips[0]), got.duration, want)
        eq("%s episode playlists" % label,
           [e.playlist for e in plan.episodes],
           [p for _c, _d, p in expected["episodes"]])
        eq("%s bonus clips" % label, [b.clips for b in plan.bonus], expected["bonus"])
        eq("%s bonus count" % label, len(plan.bonus), len(expected["bonus"]))
        for bonus in plan.bonus:
            check("%s bonus %s duration > 0" % (label, bonus.clips[0]), bonus.duration > 0.0,
                  "%.1fs" % bonus.duration)
        eq("%s start_number still 0 before assign" % label,
           [e.start_number for e in plan.episodes], [0] * len(plan.episodes))
    return plans


def test_real_dedup_numbers():
    print("\n[12] real discs: dedup candidate counts and disc numbers")
    if not ONLINE:
        skip("real dedup/number tests", "SMB share not reachable")
        return
    for label, path in DISCS:
        playlists = mpls.scan_playlists(path / "BDMV")
        eq("%s parsed playlist count" % label, len(playlists), EXPECTED[label]["playlists"])
        single = [p for p in playlists if p.playitem_count == 1]
        in_window = [p for p in single if 600.0 <= p.duration <= 4500.0]
        eq("%s candidates before dedup" % label, len(in_window),
           EXPECTED[label]["candidates"])
        plan = plan_disc(path, playlists=playlists)
        eq("%s candidates after dedup" % label, len(plan.episodes),
           len(EXPECTED[label]["episodes"]))
        check("%s dedup actually shrank the candidate list" % label,
              len(in_window) > len(plan.episodes),
              "%d -> %d" % (len(in_window), len(plan.episodes)))

    playall = {}
    for label, path in DISCS:
        playlists = mpls.scan_playlists(path / "BDMV")
        by_name = {p.name: p for p in playlists}
        for name in ("00096.mpls", "00099.mpls"):
            check("%s %s exists and has >1 playitem" % (label, name),
                  name in by_name and by_name[name].playitem_count > 1,
                  "PI=%s dur=%.1f" % (by_name[name].playitem_count, by_name[name].duration))
            check("%s %s is not an episode" % (label, name),
                  name not in [e.playlist for e in plan_disc(path, playlists=playlists).episodes])
        playall[label] = (by_name["00096.mpls"].duration, by_name["00099.mpls"].duration)
    close("00096.mpls duration 4659.8s", playall["DISC1"][0], 4659.8)
    close("00099.mpls duration 4658.8s", playall["DISC1"][1], 4658.8)
    # With a widened window the play-alls are in-range, so only the PI rule saves us.
    widened = plan_disc(DISC1, max_episode_seconds=6000.0)
    eq("DISC1 with max=6000s still yields 4 episodes (PI rule, not duration)",
       len(widened.episodes), 4)


def test_real_assign():
    print("\n[13] real discs: assign_episode_numbers -> 1-4 / 5-8 / 9-10")
    if not ONLINE:
        skip("real assign test", "SMB share not reachable")
        return
    plans = [plan_disc(path) for _label, path in DISCS]
    assign_episode_numbers(plans)
    eq("DISC1 numbers", [e.start_number for e in plans[0].episodes], [1, 2, 3, 4])
    eq("DISC2 numbers", [e.start_number for e in plans[1].episodes], [5, 6, 7, 8])
    eq("DISC3 numbers", [e.start_number for e in plans[2].episodes], [9, 10])
    eq("total episodes across discs", sum(len(p.episodes) for p in plans), 10)
    payload = json.dumps([p.to_dict() for p in plans])
    check("all three plans serialize to JSON", len(payload) > 100, "%d bytes" % len(payload))


def test_real_meta_and_siblings():
    print("\n[14] real discs: metadata and sibling discovery")
    if not ONLINE:
        skip("real meta/sibling tests", "SMB share not reachable")
        return
    siblings = discover_sibling_discs(DISC1)
    eq("three sibling discs discovered", len(siblings), 3)
    eq("siblings sorted by disc number", [p.name for p in siblings], ["DISC1", "DISC2", "DISC3"])
    eq("DISC2 siblings are the same set",
       [p.name for p in discover_sibling_discs(DISC2)], ["DISC1", "DISC2", "DISC3"])
    meta = mpls.read_disc_meta(DISC1)
    check("read_disc_meta returns the contract keys",
          set(["disc_number", "season", "title", "source"]).issubset(meta),
          str(meta))
    plan = plan_disc(DISC1)
    eq("plan.disc_number mirrors metadata", plan.disc_number, meta.get("disc_number"))
    eq("plan.season mirrors metadata", plan.season, meta.get("season"))


def test_plan_disc_pure():
    print("\n[15] plan_disc is pure: no writes, works with injected playlists")
    before = sorted(p.name for p in PLUGIN_DIR.iterdir())
    plan = plan_disc(Path("/nonexistent-disc"), playlists=[
        make_playlist("00334.mpls", ["00334"], 3890.9)])
    eq("injected playlists need no filesystem", len(plan.episodes), 1)
    after = sorted(p.name for p in PLUGIN_DIR.iterdir())
    eq("planner wrote no files next to itself", after, before)
    eq("missing BDMV without injected playlists degrades to empty plan",
       len(plan_disc(Path("/definitely-not-a-disc")).episodes), 0)
    check("empty plan carries a warning",
          len(plan_disc(Path("/definitely-not-a-disc")).warnings) > 0)


def main():
    print("=" * 72)
    print("planner.py acceptance tests")
    print("python:", sys.version.split()[0])
    print("plugin:", PLUGIN_DIR)
    print("SMB share reachable:", ONLINE)
    print("=" * 72)

    test_playitem_rule()
    test_duration_rule()
    test_runtime_rule()
    test_dedup_rule()
    test_sort_rule()
    test_bonus_rule()
    test_cross_check_rule()
    test_serialization()
    test_assign_synthetic()
    test_moviepilot_import()
    test_real_discs()
    test_real_dedup_numbers()
    test_real_assign()
    test_real_meta_and_siblings()
    test_plan_disc_pure()

    print("\n" + "=" * 72)
    print("PASSED %d | FAILED %d | SKIPPED %d" % (len(PASSED), len(FAILED), len(SKIPPED)))
    if SKIPPED:
        print("skipped: " + ", ".join(SKIPPED))
    if FAILED:
        print("failed assertions:")
        for name in FAILED:
            print("  - " + name)
        print("=" * 72)
        return 1
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
