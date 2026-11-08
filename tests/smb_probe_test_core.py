import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_core():
    path = Path(__file__).parents[1] / "plugins.v3/smblinkprobe/core.py"
    spec = importlib.util.spec_from_file_location("smb_probe_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_same_identity_and_distinct_control():
    core = load_core()
    data = {"source": (1, 20), "link": (1, 20), "control": (1, 21)}
    result = core.compare_identities(lambda p: SimpleNamespace(st_dev=data[p][0], st_ino=data[p][1]),
                                     "source", "link", "control")
    assert result == {"same_hardlink": True, "control_distinct": True, "stable_on_repeat": True}


def test_invalid_or_constant_id_never_reports_success():
    core = load_core()
    with pytest.raises(ValueError):
        core.compare_identities(lambda p: SimpleNamespace(st_dev=1, st_ino=0), "a", "b", "c")
    result = core.compare_identities(lambda p: SimpleNamespace(st_dev=1, st_ino=20), "a", "b", "c")
    assert not result["control_distinct"]


def test_changes_between_reads_are_detected():
    core = load_core()
    calls = iter((1, 1, 2, 3, 3, 2))
    result = core.compare_identities(lambda p: SimpleNamespace(st_dev=1, st_ino=next(calls)), "a", "b", "c")
    assert not result["stable_on_repeat"]


def test_remote_errors_are_not_converted_to_non_matches():
    core = load_core()

    def unreachable(path):
        raise ConnectionError("remote unavailable")

    with pytest.raises(ConnectionError):
        core.compare_identities(unreachable, "a", "b", "c")
