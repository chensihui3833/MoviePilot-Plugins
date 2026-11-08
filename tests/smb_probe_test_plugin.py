import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def test_install_does_not_connect_and_manual_run_only_stats(monkeypatch):
    plugin_dir = Path(__file__).parents[1] / "plugins.v3/smblinkprobe"
    logs = []
    changes = []
    stats = []
    base = ModuleType("app.sdk.plugin.base")
    base._PluginBase = type("Base", (), {"update_config": lambda self, config: changes.append(config)})
    log = ModuleType("app.runtime.log")
    log.logger = SimpleNamespace(info=lambda *args: logs.append(args), error=lambda *args: logs.append(args))
    storage_module = ModuleType("app.modules.filemanager.storages.smb")

    class FakeStorage:
        @classmethod
        def get_existing_instance(cls):
            changes.append("reuse")
            return cls()

        def _check_connection(self):
            pass

        def _normalize_path(self, path):
            return path

    storage_module.SMB = FakeStorage
    smbclient = ModuleType("smbclient")

    def fake_stat(path):
        stats.append(path)
        ino = 2 if path.endswith("control.txt") else 1
        return SimpleNamespace(st_dev=9, st_ino=ino)

    smbclient.stat = fake_stat
    for name, module in (("app.sdk.plugin.base", base), ("app.runtime.log", log),
                         ("app.modules.filemanager.storages.smb", storage_module), ("smbclient", smbclient)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("smblinkprobe", plugin_dir / "__init__.py",
                                                 submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "smblinkprobe", module)
    spec.loader.exec_module(module)
    probe = module.SmbLinkProbe()
    probe.init_plugin(None)
    assert changes == [] and stats == []
    probe.init_plugin({"run_once": True})
    assert changes == [{"run_once": False}, "reuse"]
    assert len(stats) == 6
    assert all(path.startswith(module.TEST_DIR + "/") for path in stats)
    assert logs[0][1:] == (True, True, True)
