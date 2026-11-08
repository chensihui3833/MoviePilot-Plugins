"""使用宿主已连接的指定 QB 实例；不读取或保存认证配置。"""

from collections.abc import Sequence
import json
import math
from pathlib import Path
import time

from .torrent_policy import inventory, source_path

PAUSED_STATES = frozenset({"pausedup", "pauseddl", "stoppedup", "stoppeddl", "paused"})
SAFE_STATES = frozenset({"uploading", "stalledup", "forcedup", "queuedup", "pausedup", "stoppedup", "paused"})


class QBUnavailable(RuntimeError):
    pass


class QBBridge:
    """所有查询都保留客户端的错误标志；删除种子时永不请求删除文件。"""

    def __init__(self, module, name):
        if not isinstance(name, str) or not name.strip():
            raise QBUnavailable("必须指定下载器")
        self.module, self.name = module, name
        self.client = module.get_instance(name)
        if not self.client:
            raise QBUnavailable("指定的 QB 下载器不可用")

    def _query(self, ids=None):
        result = self.client.get_torrents(ids=ids)
        if not isinstance(result, tuple) or len(result) != 2:
            raise QBUnavailable("QB 任务查询结果无效")
        rows, error = result
        if error or not isinstance(rows, Sequence):
            raise QBUnavailable("QB 查询失败")
        return rows

    def tasks_for(self, candidates, known_retired=None):
        candidates = {source_path(p) for p in candidates}
        known_retired = known_retired or {}
        tasks, seen = [], set()
        for row in self._query():
            tid, save = row.get("hash"), row.get("save_path")
            if not isinstance(tid, str) or not isinstance(save, str) or not save.startswith("/"):
                raise QBUnavailable("QB 任务缺少哈希或绝对保存路径")
            if tid.lower() in seen:
                raise QBUnavailable("QB 返回重复任务")
            seen.add(tid.lower())
            mapped = self.module.normalize_return_path(Path(save), self.name)
            if not isinstance(mapped, str):
                raise QBUnavailable("QB 保存路径映射结果无效")
            if not mapped.startswith("/") or mapped.startswith("//"):
                continue
            root = mapped.rstrip("/")
            if not any(p.startswith(root + "/") for p in candidates):
                continue
            files = self.client.get_files(tid=tid)
            if not isinstance(files, Sequence) or not files:
                raise QBUnavailable("QB 文件清单不可用")
            task = inventory(tid, mapped, list(files))
            if not (set(task["files"]) & candidates):
                continue
            state, progress = str(row.get("state") or "").lower(), row.get("progress")
            prior = known_retired.get(task["hash"]) or {}
            retired = prior.get("retired") or []
            tracked_incomplete = (state in PAUSED_STATES and not prior.get("removed")
                                  and prior.get("files") == task["files"] and bool(retired)
                                  and set(retired) <= set(task["files"]))
            if (not isinstance(progress, (int, float)) or not math.isfinite(progress)
                    or (not tracked_incomplete and (state not in SAFE_STATES or progress < 1))):
                raise QBUnavailable("关联任务仍在下载、校验、移动或状态不明")
            task.update({"state": state, "progress": progress})
            tasks.append(task)
        return tasks

    def verify_task(self, expected):
        """核验指定任务仍存在、已暂停、保存路径和全部视频清单未变化。"""
        rows = self._query(ids=expected["hash"])
        if len(rows) != 1:
            return False
        row = rows[0]
        if str(row.get("hash") or "").lower() != expected["hash"]:
            return False
        if str(row.get("state") or "").lower() not in PAUSED_STATES:
            return False
        save = row.get("save_path")
        if not isinstance(save, str) or not save.startswith("/"):
            return False
        mapped = self.module.normalize_return_path(Path(save), self.name)
        files = self.client.get_files(tid=expected["hash"])
        if not isinstance(files, Sequence) or not files:
            return False
        return inventory(expected["hash"], mapped, list(files))["files"] == expected["files"]

    def get_task(self, torrent_hash):
        """查询单个任务；None 只表示查询成功但该任务已不存在。"""
        rows = self._query(ids=torrent_hash)
        if not rows:
            return None
        if len(rows) != 1 or str(rows[0].get("hash") or "").lower() != torrent_hash:
            raise QBUnavailable("QB 返回的任务不唯一")
        save = rows[0].get("save_path")
        if not isinstance(save, str) or not save.startswith("/"):
            raise QBUnavailable("QB 任务保存路径无效")
        mapped = self.module.normalize_return_path(Path(save), self.name)
        files = self.client.get_files(tid=torrent_hash)
        if not isinstance(files, Sequence) or not files:
            raise QBUnavailable("QB 文件清单不可用")
        return inventory(torrent_hash, mapped, list(files))

    def pause(self, task):
        if not self.client.stop_torrents(ids=task["hash"]):
            return False
        for _ in range(3):
            if self.verify_task(task):
                return True
            time.sleep(0.25)
        return False

    def remove(self, task):
        if not self.verify_task(task):
            return False
        if not self.client.delete_torrents(ids=task["hash"], delete_file=False):
            return False
        for _ in range(3):
            rows = self._query(ids=task["hash"])
            if not rows:
                return True
            time.sleep(0.25)
        return False


class QBFleet:
    """将多个下载器的同哈希任务视为不同任务；任一查询失败均中止。"""

    def __init__(self, bridges):
        if len(bridges) < 2 or any(not isinstance(name, str) or not name for name in bridges):
            raise QBUnavailable("多个下载器的配置无效")
        self.bridges = dict(bridges)

    @staticmethod
    def _key(name, tid):
        return json.dumps([name, tid], ensure_ascii=False, separators=(",", ":"))

    def _decode(self, key):
        try:
            pair = json.loads(key)
        except (TypeError, ValueError):
            raise QBUnavailable("下载器任务标识无效") from None
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(part, str) and part for part in pair)
                or pair[0] not in self.bridges or self._key(*pair) != key):
            raise QBUnavailable("下载器任务标识无效")
        return pair

    def _unwrap(self, task):
        name, tid = self._decode(task.get("hash"))
        return self.bridges[name], {**task, "hash": tid}

    def tasks_for(self, candidates, known_retired=None):
        history = known_retired or {}
        groups = {name: {} for name in self.bridges}
        for key, record in history.items():
            name, tid = self._decode(key)
            groups[name][tid] = record
        result = []
        for name, bridge in self.bridges.items():
            for task in bridge.tasks_for(candidates, groups[name]):
                result.append({**task, "hash": self._key(name, task["hash"])})
        return result

    def pause(self, task):
        bridge, inner = self._unwrap(task)
        return bridge.pause(inner)

    def verify_task(self, task):
        bridge, inner = self._unwrap(task)
        return bridge.verify_task(inner)

    def remove(self, task):
        bridge, inner = self._unwrap(task)
        return bridge.remove(inner)

    def get_task(self, key):
        name, tid = self._decode(key)
        task = self.bridges[name].get_task(tid)
        return {**task, "hash": key} if task is not None else None


def existing_qb(name):
    """仅复用宿主已启动的 QB 模块，不创建下载器连接。"""
    from app.application.module import get_module_manager
    modules = [m for m in get_module_manager().get_running_modules("list_torrents")
               if m.get_name() == "Qbittorrent"]
    if len(modules) != 1:
        raise QBUnavailable("QB 模块不可用或不唯一")
    return QBBridge(modules[0], name)
