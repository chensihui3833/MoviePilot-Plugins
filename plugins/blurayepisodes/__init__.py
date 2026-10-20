from threading import Lock

from app.sdk.plugin import _PluginBase

from .native import native_transfer
from .path_adapter import install as install_path_adapter
from .preview_adapter import install as install_preview_adapter


class BlurayEpisodes(_PluginBase):
    plugin_name = "蓝光电视剧分集"
    plugin_desc = "读取原盘菜单与播放列表，接入自动和手动整理，生成逐集硬链接与 NFO。"
    plugin_icon = ""
    plugin_version = "0.2.4"
    plugin_author = "chensihui3833"
    author_url = "https://github.com/chensihui3833"
    plugin_config_prefix = "blurayepisodes_"
    plugin_order = 50
    auth_level = 1
    _run_lock = Lock()

    def init_plugin(self, config=None):
        self.stop_service()
        self._config = dict(config or {})
        self._enabled = bool(self._config.get("enabled", False))
        self._preview_plans = {}
        if self._enabled:
            self._restore_path_adapter = install_path_adapter(self)
            self._restore_preview_adapter = install_preview_adapter(self)

    def get_module(self):
        return {"transfer": self.transfer}

    def transfer(self, fileitem, meta, mediainfo, target_directory=None,
                 target_storage=None, target_path=None, transfer_type=None,
                 scrape=None, library_type_folder=None, library_category_folder=None,
                 episodes_info=None, source_oper=None, target_oper=None, preview=False):
        return native_transfer(
            self, fileitem, meta, mediainfo, target_directory, target_storage,
            target_path, transfer_type, scrape, library_type_folder,
            library_category_folder, episodes_info, source_oper, target_oper, preview,
        )

    def get_state(self):
        return bool(getattr(self, "_enabled", False))

    @staticmethod
    def get_api():
        return []

    @staticmethod
    def get_command():
        return []

    def get_form(self):
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {
                "model": "enabled", "label": "启用蓝光电视剧自动分集"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": "启用后接入 MoviePilot 原有自动整理和手动整理，沿用目标目录及命名规则。支持 Sony/onQ 菜单完整的一季 BDMV，按每盘生成逐集硬链接和 NFO；关闭后恢复原有流程。无需另填源目录或逐次点导出。"},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
             "text": "完整一季的 DISC1、DISC2… 必须在同一目录，源与目标须在同一文件系统。信息不足或文件冲突会失败并提示，不回退整盘整理。原生重新整理可能在插件执行前清理旧目标；部署前须核对联动删除插件。"},
        ]}], {"enabled": False}

    def get_page(self):
        plan = self.get_data("preview") or {}
        header = ["集号", "原盘标题", "盘号", "盘内集号", "播放列表", "视频片段", "分钟"]
        rows = []
        for item in plan.get("episodes", []):
            values = [
                f"S{plan['season']:02}E{item['episode']:02}", item["title"], item["disc"],
                item["disc_episode"], item["playlist"] + ".mpls", item["clip"] + ".m2ts",
                round(item["seconds"] / 60, 2),
            ]
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(value)} for value in values
            ]})
        return [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": self.get_data("status") or "启用后请从 MoviePilot 原来的整理入口使用；这里显示最近一盘的分集结果。"},
            {"component": "div", "text": plan.get("mapping_basis", "")},
            {"component": "VTable", "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": value} for value in header
                ]}]},
                {"component": "tbody", "content": rows},
            ]},
        ]

    def stop_service(self):
        self._enabled = False
        restore = getattr(self, "_restore_path_adapter", None)
        if restore:
            restore()
            self._restore_path_adapter = None
        restore = getattr(self, "_restore_preview_adapter", None)
        if restore:
            restore()
            self._restore_preview_adapter = None
