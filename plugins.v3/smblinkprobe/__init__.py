"""临时只读诊断：通过 MoviePilot 已配置 SMB 验证硬链接文件标识。"""

from app.runtime.log import logger
from app.sdk.plugin.base import _PluginBase

from .core import compare_identities

TEST_DIR = "/Codex-SMB-Probe-20261108-a61d"
TEST_FILES = ("source.txt", "hardlink.txt", "control.txt")


class SmbLinkProbe(_PluginBase):
    plugin_name = "SMB 硬链接诊断"
    plugin_desc = "只读取专用测试目录的文件标识，不删除文件。"
    plugin_version = "0.1.0"
    plugin_author = "chensihui3833"
    plugin_config_prefix = "smblinkprobe_"
    plugin_order = 99
    auth_level = 1

    def init_plugin(self, config=None):
        if not (config or {}).get("run_once"):
            return
        self.update_config({"run_once": False})
        try:
            from app.modules.filemanager.storages.smb import SMB
            import smbclient

            # 只复用已存活的 SMB 连接，不创建连接或访问存储配置。
            storage = SMB.get_existing_instance()
            if storage is None:
                raise RuntimeError("MoviePilot 中没有已初始化的 SMB 连接")
            storage._check_connection()
            paths = [storage._normalize_path(f"{TEST_DIR}/{name}") for name in TEST_FILES]
            result = compare_identities(smbclient.stat, *paths)
            logger.info("SMB 硬链接诊断：硬链接标识相同=%s，对照文件不同=%s，重复读取一致=%s",
                        result["same_hardlink"], result["control_distinct"], result["stable_on_repeat"])
        except Exception as error:
            # 异常文本有可能包含连接信息；仅输出类别，不输出路径、账号或认证内容。
            logger.error("SMB 硬链接诊断失败，错误类别：%s", type(error).__name__)

    def get_state(self):
        return False

    def stop_service(self):
        pass

    def get_api(self):
        return []

    def get_form(self):
        return ([{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VSwitch", "props": {"model": "run_once", "label": "只读运行一次"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": "只检查预先建立的专用测试文件，结果见插件日志，不修改或删除任何文件。"}
        ]}]}], {"run_once": False})

    def get_page(self):
        return []
