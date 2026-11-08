"""极空间本地硬链接的保守扫描及 QB 种子联动。"""
import hashlib
import os
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.runtime.log import logger
from app.sdk.plugin.base import _PluginBase

from .core import VIDEO_EXTENSIONS, calculate, file_key, parse_pairs
from .qb_bridge import QBUnavailable, QBFleet, existing_qb
from .torrent_coordinator import CoordinationError, finalize_ready, run_candidate

SESSION_ID = uuid.uuid4().hex
CONFIRM_SECONDS = 600
MAX_FILES = 50000


def local_identity(info):
    """本地硬链接增删会改变 ctime；使用设备号、inode、大小与修改时间。"""
    values = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if not all(isinstance(value, int) for value in values) or info.st_ino <= 0 or info.st_size < 0:
        raise ValueError("本地文件标识不可用")
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": 0}


def verify_directory(path):
    """检查路径及祖先均为目录，不接受符号链接。"""
    current = Path("/")
    for part in Path(path).parts[1:]:
        current /= part
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("监控路径包含链接或非目录")


def _walk_error(error):
    raise error


def scan_tree(root, max_files=MAX_FILES):
    root = str(PurePosixPath(root))
    verify_directory(root)
    result = {}
    seen = False
    for current, dirs, files in os.walk(root, topdown=True, onerror=_walk_error, followlinks=False):
        seen = True
        verify_directory(current)
        entries = list(os.scandir(current))
        if {entry.name for entry in entries} != set(dirs) | set(files):
            raise OSError("本地目录在扫描期间发生变化")
        for entry in entries:
            if entry.is_symlink():
                raise ValueError("发现符号链接，扫描已中止")
        for name in dirs:
            info = os.lstat(os.path.join(current, name))
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("目录在扫描时发生变化")
        for name in files:
            if PurePosixPath(name).suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("视频在扫描时发生变化")
            key = path.casefold()
            if key in result:
                raise ValueError("文件名存在大小写冲突")
            result[key] = {"path": path, **local_identity(info)}
            if len(result) > max_files:
                raise ValueError("扫描文件数超过上限")
    if not seen:
        raise OSError("本地目录未能读取")
    return result


def delete_verified(path, expected, recheck):
    """打开文件并多次比较路径与句柄身份，复核通过后删除该名称。"""
    parent, name = os.path.split(path)
    verify_directory(parent)
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        for part in Path(parent).parts[1:]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        def same():
            verify_directory(parent)
            parent_info = os.stat(parent, follow_symlinks=False)
            directory_info = os.fstat(directory)
            opened = os.fstat(fd)
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
            return ((parent_info.st_dev, parent_info.st_ino) == (directory_info.st_dev, directory_info.st_ino)
                    and stat.S_ISREG(opened.st_mode) and stat.S_ISREG(named.st_mode)
                    and local_identity(opened) == {k: expected[k] for k in local_identity(opened)}
                    and local_identity(named) == local_identity(opened))
        if not same() or not recheck() or not same():
            return False
        os.unlink(name, dir_fd=directory)
        return True
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


class LocalLinkClean(_PluginBase):
    plugin_name = "本地硬链接安全清理"
    plugin_desc = "监控本地媒体库替换，并安全联动 QB 暂停或删除种子。"
    plugin_version = "0.1.1"
    plugin_author = "chensihui3833"
    plugin_config_prefix = "locallinkclean_"
    plugin_order = 56
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
            self._force_baseline |= bool(cfg.get("enabled") and not self._config.get("enabled"))
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
        return [{"id": "local_link_clean", "name": "本地硬链接安全清理",
                 "trigger": "interval", "func": self.scan, "kwargs": {"minutes": minutes}}]

    def scan(self):
        if not self._lock.acquire(blocking=False):
            return
        generation = self._generation
        removed = removed_tasks = skipped = 0
        try:
            pairs = parse_pairs(self._config.get("pairs") or "")
            scope = hashlib.sha256("\0".join(sorted(a + "\0" + b for a, b in pairs)).encode()).hexdigest()
            previous = None if self._force_baseline else self.get_data("snapshot")
            def scan_all(index):
                combined = {}
                for pair in pairs:
                    found = scan_tree(pair[index], MAX_FILES - len(combined))
                    if set(combined) & set(found):
                        raise ValueError("监控目录重叠")
                    combined.update(found)
                return combined
            sources, library = scan_all(0), scan_all(1)
            current = {"scope": scope, "session": SESSION_ID, "epoch": time.monotonic(),
                       "sources": sources, "library": library}
            if previous and previous.get("session") == SESSION_ID and current["epoch"] < previous.get("epoch", 0):
                raise ValueError("系统时间回退")
            current["candidates"] = calculate(previous, current)
            current["time"] = datetime.now(timezone.utc).isoformat()
            if generation != self._generation:
                return
            if self._config.get("enabled") and self._config.get("auto_delete") and current["library"]:
                names = self._config.get("qb_names")
                if not isinstance(names, list) or not names or any(not isinstance(x, str) or not x or "\0" in x for x in names) or len(names) != len(set(names)):
                    raise CoordinationError("请选择有效的 QB 下载器")
                names = sorted(names)
                bridges = {name: existing_qb(name) for name in names}
                qb = bridges[names[0]] if len(names) == 1 else QBFleet(bridges)
                journal_scope = hashlib.sha256((scope + "\0" + "\0".join(names)).encode()).hexdigest()
                journal = self.get_data("qb_journal") or {"scope": journal_scope, "tasks": {}, "intents": {}}
                if journal.get("scope") != journal_scope:
                    if (any(not task.get("removed") for task in journal.get("tasks", {}).values())
                            or any(intent.get("state") == "pending" for intent in journal.get("intents", {}).values())):
                        raise CoordinationError("下载器或目录变更，待处理种子需要人工核对")
                    journal = {"scope": journal_scope, "tasks": {}, "intents": {}}
                candidates = {item["source"].casefold() for item in current["candidates"]}
                if any(intent.get("state") == "pending" and path.casefold() not in candidates
                       for path, intent in journal.get("intents", {}).items()):
                    raise CoordinationError("存在未完成的清理记录")
                def save_journal(value):
                    self.save_data("qb_journal", value)
                def source_absent(path):
                    if not any(path.startswith(root + "/") for root, _ in pairs):
                        raise CoordinationError("待核对源文件超出监控目录")
                    return path.casefold() not in scan_tree(str(PurePosixPath(path).parent))
                with self._control_lock:
                    if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                        return
                    removed_tasks += finalize_ready(qb, journal, save_journal, source_absent)
                for candidate in list(current["candidates"]):
                    if candidate["source_key"] not in current["sources"] or candidate.get("observations", 0) < 2 or current["epoch"] - candidate.get("first_seen", current["epoch"]) < CONFIRM_SECONDS:
                        continue
                    source = candidate["source"]
                    if not any(source.startswith(root + "/") for root, _ in pairs):
                        raise ValueError("源文件超出监控范围")
                    expected = candidate["identity"]
                    parent = str(PurePosixPath(candidate["library"]).parent)
                    if not any(parent == root or parent.startswith(root + "/") for _, root in pairs):
                        raise ValueError("媒体文件超出监控范围")
                    def recheck_library(qb_check):
                        if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                            return False
                        fresh, old_dir = scan_all(1), scan_tree(parent)
                        return (len(fresh) >= len(current["library"])
                                and file_key(expected) not in {file_key(item) for item in fresh.values()}
                                and file_key(expected) not in {file_key(item) for item in old_dir.values()}
                                and qb_check() and generation == self._generation
                                and self._config.get("enabled") and self._config.get("auto_delete"))
                    with self._control_lock:
                        if generation != self._generation or not self._config.get("enabled") or not self._config.get("auto_delete"):
                            return
                        result = run_candidate(qb, candidate, journal, save_journal,
                                               lambda qb_check: delete_verified(source, expected, lambda: recheck_library(qb_check)),
                                               source_absent)
                    removed_tasks += result["removed_tasks"]
                    if result["cleaned"]:
                        removed += 1
                        current["sources"].pop(candidate["source_key"], None)
                        current["candidates"] = [x for x in current["candidates"] if x["source_key"] != candidate["source_key"]]
                    else:
                        skipped += 1
            if generation != self._generation:
                return
            self.save_data("snapshot", current)
            self._force_baseline = False
            self.save_data("status", {"ok": True, "time": current["time"], "baseline": not previous or previous.get("scope") != scope or previous.get("session") != SESSION_ID,
                                      "candidate_count": len(current["candidates"]), "removed": removed, "removed_tasks": removed_tasks, "skipped": skipped})
            logger.info("本地硬链接扫描完成：候选 %s，已清理 %s，已删种 %s，跳过 %s", len(current["candidates"]), removed, removed_tasks, skipped)
        except Exception as error:
            status = {"ok": False, "error": type(error).__name__, "removed": removed, "removed_tasks": removed_tasks}
            if isinstance(error, (CoordinationError, QBUnavailable)):
                status["detail"] = str(error)
            self.save_data("status", status)
            logger.error("本地硬链接扫描失败（%s），已确认清理 %s 个", type(error).__name__, removed)
        finally:
            self._lock.release()

    def get_form(self):
        try:
            from app.sdk.services import ServiceConfigHelper
            options = sorted({conf.name for conf in ServiceConfigHelper.get_downloader_configs()
                              if conf.enabled and str(conf.type or "").lower() == "qbittorrent" and conf.name})
        except Exception:
            options = []
        return ([{"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"}, "text": "仅扫描本地目录。自动清理将暂停对应 QB 种子；合集仍有视频时保留任务和其他文件。"}]},
            *[{"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}]} for model, label in (("enabled", "启用定期扫描"), ("auto_delete", "自动清理旧源文件"), ("run_once", "保存后扫描一次"))],
            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{"component": "VSelect", "props": {"model": "qb_names", "label": "联动 QB 下载器", "items": [{"title": name, "value": name} for name in options], "multiple": True, "chips": True}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VTextField", "props": {"model": "interval", "label": "扫描间隔（分钟）", "type": "number"}}]},
            {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "pairs", "label": "本地目录对", "rows": 3, "hint": "每行一组：下载目录 | 媒体库目录", "persistent-hint": True}}]},
        ]}], {"enabled": False, "auto_delete": False, "run_once": False, "qb_names": [], "pairs": "", "interval": 30})

    def get_page(self):
        status = self.get_data("status") or {}
        snap = self.get_data("snapshot") or {}
        if not status:
            text = "尚未扫描。"
        elif not status.get("ok"):
            text = f"最近扫描失败（{status.get('detail') or status.get('error')}），请核对是否部分完成。"
        elif status.get("baseline"):
            text = "已建立本地目录基线，本次没有删除文件。"
        else:
            text = f"候选 {status.get('candidate_count', 0)}，清理 {status.get('removed', 0)}，删除种子 {status.get('removed_tasks', 0)}。"
        items = [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"}, "text": text}]
        for candidate in snap.get("candidates", [])[:50]:
            items.append({"component": "VAlert", "props": {"type": "warning"}, "text": f"{candidate['reason']}：{candidate['library']} → {candidate['source']}"})
        return items
