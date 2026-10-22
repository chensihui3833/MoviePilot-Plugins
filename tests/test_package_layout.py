import ast
import json
import unittest
from pathlib import Path


class PackageLayoutTest(unittest.TestCase):
    def test_v3_catalog_and_legacy_layout_match(self):
        root = Path(__file__).parents[1]
        legacy = root / "plugins/blurayepisodes"
        native = root / "plugins.v3/blurayepisodes"
        files = sorted(p.name for p in legacy.glob("*.py"))
        self.assertEqual(files, sorted(p.name for p in native.glob("*.py")))
        for name in files:
            self.assertEqual((legacy / name).read_bytes(), (native / name).read_bytes(), name)
        one = json.loads((root / "package.json").read_text())["BlurayEpisodes"]
        three = json.loads((root / "package.v3.json").read_text())["BlurayEpisodes"]
        self.assertEqual(one, three)
        parsed = ast.parse((native / "__init__.py").read_text())
        versions = [n.value.value for c in parsed.body if isinstance(c, ast.ClassDef)
                    for n in c.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "plugin_version" for t in n.targets)]
        self.assertEqual(versions, [three["version"]])
        self.assertFalse(three["v2"])
        self.assertTrue(three["v3"])
