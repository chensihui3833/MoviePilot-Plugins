"""候选重取后优先级修复；不改写主程序文件。"""

import ast
from hashlib import sha256
from pathlib import Path

from app.chain.search import scan as scan_module
from app.runtime.log import logger
from app.sdk.plugin.base import _PluginBase


SUPPORTED_METHOD_SHA256 = "1cf4d46e2bdb2092ab9c6f20a5792c2912871d28f9748a87decf6d8f75363473"


def _method_digest(path):
    """只校验补丁依赖的方法结构，不按主程序版本或整个文件限制。"""
    source = Path(path).read_text(encoding="utf-8")
    module = ast.parse(source)
    owner = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SearchScan")
    methods = [next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == name)
               for name in ("_prepared_batch", "_filter", "_coverage")]
    text = "\n".join(ast.get_source_segment(source, node) for node in methods)
    return sha256(text.encode()).hexdigest()


class QualityPriorityFix(_PluginBase):
    plugin_name = "洗版候选优先级修复"
    plugin_desc = "重取站点页面后重新筛选候选并计算优先级，仅适配已验证的主程序源码。"
    plugin_version = "0.2.0"
    plugin_author = "chensihui3833"
    plugin_config_prefix = "qualitypriorityfix_"
    plugin_order = 1
    auth_level = 1

    def init_plugin(self, config=None):
        self.stop_service()
        self._enabled = bool((config or {}).get("enabled", False))
        self._status = "未启用"
        if not self._enabled:
            return
        scan_class = scan_module.SearchScan
        original = scan_class._prepared_batch
        try:
            digest = _method_digest(scan_module.__file__)
        except (OSError, TypeError, SyntaxError, UnicodeError, StopIteration) as error:
            self._status = f"无法验证主程序源码，修复未生效：{error}"
            self._warn()
            return
        if (digest != SUPPORTED_METHOD_SHA256
                or getattr(original, "__module__", None) != scan_module.__name__
                or getattr(original, "__qualname__", None) != "SearchScan._prepared_batch"):
            self._status = "主程序源码不在已验证范围，或检测到重复修复；修复未生效，请检查升级兼容性"
            self._warn()
            return

        def prepared_batch(scan, contexts, ready):
            prepared, _ = original(scan, contexts, ready)
            prepared = scan._filter(prepared)
            goals = {target for item in prepared if item.torrent_info is not None
                     for target in scan._coverage(item.torrent_info.search_resource_id)}
            return prepared, goals

        prepared_batch.__quality_fix_wrapper__ = True
        self._original = original
        self._wrapper = prepared_batch
        scan_class._prepared_batch = prepared_batch
        self._status = "已生效：候选重取后重新筛选和计算优先级"
        logger.info(self._status)

    def _warn(self):
        logger.error(self._status)
        system_message = getattr(self, "systemmessage", None)
        if system_message is not None:
            system_message.put(self._status, title=self.plugin_name, level="warning")

    def stop_service(self):
        wrapper = getattr(self, "_wrapper", None)
        original = getattr(self, "_original", None)
        if wrapper is not None and scan_module.SearchScan._prepared_batch is wrapper:
            scan_module.SearchScan._prepared_batch = original
        self._wrapper = None
        self._original = None
        self._enabled = False
        self._status = "未启用"

    def get_state(self):
        return bool(getattr(self, "_enabled", False) and getattr(self, "_wrapper", None)
                    and scan_module.SearchScan._prepared_batch is self._wrapper)

    def get_api(self):
        return []

    def get_form(self):
        return ([{"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用优先级修复"}},
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": getattr(self, "_status", "未启用")},
                {"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                 "text": "不限制主程序版本号；关键代码变化时自动停用并提示检查。"}
            ]}
        ]}], {"enabled": False})

    def get_page(self):
        return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": getattr(self, "_status", "未启用")}]
