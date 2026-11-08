"""通过 MoviePilot 的 SMB 存储识别并可选清理旧源文件。"""

import hashlib
import ntpath
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from app.runtime.log import logger
from app.sdk.plugin.base import _PluginBase

from .core import VIDEO_EXTENSIONS, calculate, file_key, identity, parse_pairs
from .qb_bridge import existing_qb
from .torrent_coordinator import CoordinationError, finalize_ready, run_candidate
from .verified_delete import delete_verified

MAX_FILES = 50000
CONFIRM_SECONDS = 600
SESSION_ID = uuid.uuid4().hex
# Linux CIFS Minshall+French links can appear over SMB as regular files of this size.
POSSIBLE_MF_SYMLINK_SIZE = 1067


class PossibleEncodedLinkError(ValueError):
    """固定大小的普通文件可能是某些 SMB 客户端编码的链接。"""


def _raise_walk_error(error):
    raise error


def _reject_redirect(info):
    # SMB 其他重解析点也可能指向扫描范围以外，并非只有 UNIX 符号链接。
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("目录或视频文件含符号链接或重解析点，扫描已中止")


def scan_tree(client, unc_root, logical_root, max_files=MAX_FILES):
    """完整遍历共享目录；任何读取错误都使整轮结果无效。"""
    result = {}
    seen_root = False
    share, tail = ntpath.splitdrive(unc_root)
    path = share + "\\"
    for part in tail.strip("\\").split("\\"):
        path = ntpath.join(path, part)
        info = client.lstat(path)
        _reject_redirect(info)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("SMB 监控路径不是普通目录")
    for current, dirs, files in client.walk(unc_root, onerror=_raise_walk_error, follow_symlinks=False):
        seen_root = True
        relative = ntpath.relpath(current, unc_root)
        if relative == ".." or relative.startswith("..\\") or ntpath.isabs(relative):
            raise ValueError("SMB 遍历越界")
        prefix = "" if relative == "." else relative.replace("\\", "/")
        # 部分客户端把符号链接编码成普通文件。只检查目录项元数据，
        # 不打开文件内容；固定长度的普通文件无法排除链接时宁可停止。
        enumerated = set()
        for entry in client.scandir(current):
            info = entry.smb_info
            if entry.name in enumerated:
                raise ValueError("SMB 目录包含重复项目")
            enumerated.add(entry.name)
            if info.file_attributes & 0x400 or entry.is_symlink():
                raise ValueError("目录含链接或疑似链接，扫描已中止")
            if not (info.file_attributes & 0x10) and info.end_of_file == POSSIBLE_MF_SYMLINK_SIZE:
                raise PossibleEncodedLinkError("发现 1067 字节文件，可能是 SMB 编码链接，也可能是普通文件")
        if enumerated != set(dirs) | set(files):
            raise OSError("SMB 目录在扫描期间发生变化")
        for name in dirs:
            if name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
                raise ValueError("SMB 目录名无效")
            info = client.lstat(ntpath.join(current, name))
            _reject_redirect(info)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("SMB 目录在扫描时发生变化")
        for name in files:
            if name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
                raise ValueError("SMB 文件名无效")
            if PurePosixPath(name).suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            remote_path = ntpath.join(current, name)
            info = client.lstat(remote_path)
            _reject_redirect(info)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("视频文件在扫描时发生变化")
            path = str(PurePosixPath(logical_root) / prefix / name)
            key = path.casefold()
            if key in result:
                raise ValueError("共享存在名称大小写冲突")
            result[key] = {"path": path, **identity(info)}
            if len(result) > max_files:
                raise ValueError("扫描文件数超过上限")
    if not seen_root:
        raise OSError("SMB 目录未能完整读取")
    return result


class SmbLinkClean(_PluginBase):
    plugin_name = "SMB 硬链接清理"
    plugin_desc = "通过 MoviePilot 的 SMB 连接预览或清理旧下载源文件。"
    plugin_version = "0.2.1"
    plugin_author = "chensihui3833"
    plugin_config_prefix = "smblinkclean_"
    plugin_order = 55
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._control_lock = threading.RLock()
        self._generation = 0
        self._config = {}
        self._force_baseline = False

    def init_plugin(self, config=None):
        with self._control_lock:
            self._generation += 1
            cfg = dict(config or {})
            self._force_baseline = self._force_baseline or bool(cfg.get("enabled") and not self._config.get("enabled"))
            self._config = cfg
            once = bool(cfg.get("run_once"))
            if once:
                cfg["run_once"] = False
                self.update_config(cfg)
        if once:
            threading.Thread(target=self.scan, daemon=True).start()

    def get_state(self):
        return bool(self._config.get("enabled"))

    def stop_service(self):
        with self._control_lock:
            self._generation += 1
            self._config = {}
            self._force_baseline = True

    def get_api(self):
        return []

    def get_service(self):
        if not self.get_state():
            return []
        try:
            minutes = max(10, min(int(self._config.get("interval") or 30), 1440))
        except (TypeError, ValueError):
            minutes = 30
        return [{"id": "smb_link_preview", "name": "SMB 硬链接清理",
                 "trigger": "interval", "func": self.scan, "kwargs": {"minutes": minutes}}]

    def scan(self):
        if not self._lock.acquire(blocking=False):
            logger.warning("SMB 硬链接预览：上一次扫描尚未结束")
            return
        generation = self._generation
        removed = 0
        removed_tasks = 0
        skipped = 0
        try:
            pairs = parse_pairs(self._config.get("pairs") or
                                f"{self._config.get('source') or ''}|{self._config.get('library') or ''}")
            from app.modules.filemanager.storages.smb import SMB
            import smbclient

            # SMB 存储负责自己的认证连接；插件不读取或输出认证信息。
            storage = SMB()
            storage._check_connection()
            unc_pairs = [(storage._normalize_path(source), storage._normalize_path(library)) for source, library in pairs]
            drives = {ntpath.splitdrive(path)[0].casefold() for pair in unc_pairs for path in pair}
            if len(drives) != 1:
                raise ValueError("所有目录必须在同一 SMB 共享")
            scope_text = "\0".join(sorted(source.casefold() + "\0" + library.casefold() for source, library in unc_pairs))
            scope = hashlib.sha256(scope_text.encode()).hexdigest()
            previous = self.get_data("snapshot")
            if self._force_baseline:
                previous = None
            def scan_all(index):
                combined = {}
                for pair, unc_pair in zip(pairs, unc_pairs):
                    found = scan_tree(smbclient, unc_pair[index], pair[index], max_files=MAX_FILES - len(combined))
                    if set(combined) & set(found):
                        raise ValueError("SMB 目录扫描结果重叠")
                    combined.update(found)
                return combined
            sources, library = scan_all(0), scan_all(1)
            # 同一运行会话内使用单调时钟，避免系统校时造成等待时间被跳过。
            # 等待时间从完整扫描结束后计算，不能把遍历时间算进去。
            current = {"scope": scope, "session": SESSION_ID, "epoch": time.monotonic(),
                       "sources": sources, "library": library}
            if previous and previous.get("session") == SESSION_ID and current["epoch"] < previous.get("epoch", 0):
                raise ValueError("系统时间回退，取消本轮")
            current["candidates"] = calculate(previous, current)
            current["time"] = datetime.now(timezone.utc).isoformat()
            if generation != self._generation:
                logger.warning("SMB 硬链接预览：扫描期间配置发生变化，结果已丢弃")
                return
            # 即使服务器没有报告错误，突然显示为空也可能是权限或挂载异常。
            safe_library = bool(current["library"])
            if self._config.get("enabled") and self._config.get("auto_delete") and safe_library:
                qb_name = str(self._config.get("qb_name") or "").strip()
                if not qb_name:
                    raise CoordinationError("请指定联动的 QB 下载器名称")
                qb = existing_qb(qb_name)
                journal_scope = hashlib.sha256((scope + "\0" + qb_name).encode()).hexdigest()
                journal = self.get_data("qb_journal") or {"scope": journal_scope, "tasks": {}, "intents": {}}
                if journal.get("scope") != journal_scope:
                    if any(not task.get("removed") for task in journal.get("tasks", {}).values()):
                        raise CoordinationError("下载器或目录变更，原有待处理种子需要人工核对")
                    journal = {"scope": journal_scope, "tasks": {}, "intents": {}}
                candidate_sources = {item["source"].casefold() for item in current["candidates"]}
                if any(intent.get("state") == "pending" and path.casefold() not in candidate_sources
                       for path, intent in journal.get("intents", {}).items()):
                    raise CoordinationError("存在未完成的清理记录，请先检查源文件及 QB 任务")

                def save_journal(value):
                    self.save_data("qb_journal", value)

                def source_absent(path):
                    if not any(path.casefold().startswith(root.casefold() + "/") for root, _ in pairs):
                        raise CoordinationError("待核对源文件超出监控目录")
                    parent = str(PurePosixPath(path).parent)
                    listing = scan_tree(smbclient, storage._normalize_path(parent), parent)
                    return path.casefold() not in listing

                with self._control_lock:
                    if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                        return
                    removed_tasks += finalize_ready(qb, journal, save_journal, source_absent)
                for candidate in list(current["candidates"]):
                    if candidate["source_key"] not in current["sources"]:
                        continue
                    if (candidate.get("observations", 0) < 2
                            or current["epoch"] - candidate.get("first_seen", current["epoch"]) < CONFIRM_SECONDS):
                        continue
                    source_path = candidate["source"]
                    if not any(source_path.casefold().startswith(source.casefold() + "/") for source, _ in pairs):
                        raise ValueError("候选源文件不在下载目录内")
                    source_path_unc = storage._normalize_path(source_path, writable=True)
                    expected = candidate["identity"]
                    old_parent = str(PurePosixPath(candidate["library"]).parent)
                    if not any(old_parent.casefold() == library.casefold()
                               or old_parent.casefold().startswith(library.casefold() + "/")
                               for _, library in pairs):
                        raise ValueError("候选媒体文件不在媒体库目录内")

                    def recheck_library(qb_check):
                        if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                            return False
                        fresh = scan_all(1)
                        # 单独检查旧文件所在目录，避免上层枚举把失联或被隐藏的子目录当成删除。
                        old_directory = scan_tree(smbclient, storage._normalize_path(old_parent), old_parent)
                        # 复核期间可见文件减少时无法断言扫描完整，宁可跳过。
                        return (len(fresh) >= len(current["library"])
                                and file_key(expected) not in {file_key(item) for item in fresh.values()}
                                and file_key(expected) not in {file_key(item) for item in old_directory.values()}
                                and qb_check()
                                and generation == self._generation and self._config.get("enabled")
                                and self._config.get("auto_delete"))

                    with self._control_lock:
                        if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                            return
                        result = run_candidate(qb, candidate, journal, save_journal,
                                               lambda qb_check: delete_verified(source_path_unc, expected,
                                                                                lambda: recheck_library(qb_check)),
                                               source_absent)
                    removed_tasks += result["removed_tasks"]
                    if result["cleaned"]:
                        removed += 1
                        current["sources"].pop(candidate["source_key"], None)
                        current["candidates"] = [item for item in current["candidates"]
                                                 if item["source_key"] != candidate["source_key"]]
                    else:
                        skipped += 1
                        logger.warning("SMB 硬链接清理跳过候选：%s", result["reason"])
            if generation != self._generation:
                logger.warning("SMB 硬链接预览：配置变化，结果未保存")
                return
            self.save_data("snapshot", current)
            self._force_baseline = False
            self.save_data("status", {"ok": True, "time": current["time"],
                                      "baseline": not previous or previous.get("scope") != scope or previous.get("session") != SESSION_ID,
                                      "source_count": len(current["sources"]),
                                      "library_count": len(current["library"]),
                                      "candidate_count": len(current["candidates"]), "removed": removed,
                                      "removed_tasks": removed_tasks, "skipped": skipped})
            logger.info("SMB 硬链接扫描完成：源文件 %s，媒体文件 %s，候选 %s，已清理 %s，已删除种子 %s，跳过 %s",
                        len(current["sources"]), len(current["library"]), len(current["candidates"]), removed,
                        removed_tasks, skipped)
        except Exception as error:
            # 异常可能含 SMB 路径或认证上下文，不输出异常正文。
            self.save_data("status", {"ok": False, "error": type(error).__name__,
                                      "removed": removed, "removed_tasks": removed_tasks,
                                      "time": datetime.now(timezone.utc).isoformat()})
            logger.error("SMB 硬链接扫描失败（%s），已确认清理 %s 个，未更新快照；请检查是否部分完成", type(error).__name__, removed)
        finally:
            self._lock.release()

    def get_form(self):
        return ([{"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                 "text": "自动清理会暂停匹配的 QB 种子并删除旧源文件；所有视频清理完成后删除任务并保留其余文件。"},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用定期扫描"}},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                {"component": "VSwitch", "props": {"model": "auto_delete", "label": "自动清理旧源文件"}},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                {"component": "VSwitch", "props": {"model": "run_once", "label": "保存后扫描一次"}},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                {"component": "VTextField", "props": {"model": "qb_name", "label": "联动 QB 下载器名称",
                                                     "hint": "须与 MoviePilot 下载器名称完全一致", "persistent-hint": True}},
            ]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                {"component": "VTextField", "props": {"model": "interval", "label": "扫描间隔（分钟）",
                                                     "type": "number", "hint": "至少 10 分钟", "persistent-hint": True}},
            ]},
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VTextarea", "props": {"model": "pairs", "label": "SMB 目录对", "rows": 3,
                                                    "hint": "每行一组：下载目录 | 媒体库目录", "persistent-hint": True}},
            ]},
        ]}], {"enabled": False, "auto_delete": False, "run_once": False, "qb_name": "", "pairs": "", "interval": 30})

    def get_page(self):
        status = self.get_data("status") or {}
        snap = self.get_data("snapshot") or {}
        if not status:
            text = "尚未扫描。"
        elif not status.get("ok"):
            reason = ("发现 1067 字节文件，可能是 SMB 链接或普通文件"
                      if status.get("error") == "PossibleEncodedLinkError" else status.get("error", "未知错误"))
            text = (f"最近扫描失败（{reason}），本轮确认已清理 {status.get('removed', 0)} 个；"
                    "操作可能部分完成，请核对文件。以下是上一次成功的结果，请勿据此删除。")
        elif status.get("baseline"):
            text = "初次扫描已建立基线，没有自动删除任何文件。"
        else:
            text = (f"最近一次完整扫描：{status.get('time')}；候选 {status.get('candidate_count', 0)} 个，"
                    f"已清理 {status.get('removed', 0)} 个，已删除种子 {status.get('removed_tasks', 0)} 个，跳过 {status.get('skipped', 0)} 个。")
        items = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"}, "text": text}]
        journal = self.get_data("qb_journal") or {}
        waiting = [record for record in journal.get("tasks", {}).values()
                   if record.get("retired") and not record.get("removed")]
        if waiting:
            items.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                          "text": f"有 {len(waiting)} 个已部分清理的 QB 任务等待后续处理；请检查任务是否仍暂停。"})
        for candidate in snap.get("candidates", [])[:50]:
            items.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
                          "text": f"{candidate['reason']}：{candidate['library']} → 候选源文件 {candidate['source']}"})
        if len(snap.get("candidates", [])) > 50:
            items.append({"component": "VAlert", "props": {"type": "warning"}, "text": "仅显示前 50 项。"})
        return items
