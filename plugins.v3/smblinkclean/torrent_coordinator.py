"""在 SMB 清理与 QB 任务之间保守协调；持久化意图后才动源文件。"""

from copy import deepcopy

from .torrent_policy import task_decision


class CoordinationError(RuntimeError):
    pass


def _task_records(journal, tasks):
    records = journal.setdefault("tasks", {})
    for task in tasks:
        tid, files = task["hash"], task["files"]
        old = records.get(tid)
        if old and old["files"] != files:
            raise CoordinationError("种子文件清单发生变化")
        if not old:
            records[tid] = {"files": list(files), "retired": [], "removed": False}
    return records


def _all_sources_gone(record, source_absent):
    """先要求有本插件清理记录，再逐项确认种子内的视频均已不存在。"""
    files = record.get("files")
    retired = record.get("retired") or []
    task_decision({"files": files}, (), retired)  # 校验清单与路径
    if not retired:
        return False
    if not set(retired) <= set(files):
        raise CoordinationError("清理记录不属于种子文件清单")
    return all(source_absent(path) for path in files)


def run_candidate(qb, candidate, journal, save, delete_source, source_absent):
    """确认全部关联种子暂停，持久化意图，在回调中复查后清理源文件。

    ``delete_source`` 接受一个复核回调，并须在实际删除同一文件句柄前调用。
    如果没有唯一可靠的种子对应关系或任何 QB 操作失败，源文件保留。
    """
    source = candidate["source"]
    tasks = qb.tasks_for([source], journal.get("tasks", {}))
    if not tasks:
        return {"cleaned": False, "reason": "未匹配到 QB 种子", "removed_tasks": 0}
    if len({task["hash"] for task in tasks}) != len(tasks) or any(source not in task["files"] for task in tasks):
        raise CoordinationError("QB 匹配结果不完整或不唯一")
    records = _task_records(journal, tasks)
    for task in tasks:
        if records[task["hash"]].get("removed"):
            raise CoordinationError("已删除任务再次出现，需人工核对")
        if not qb.pause(task):
            raise CoordinationError("QB 未确认暂停，源文件已保留")
    hashes = sorted(task["hash"] for task in tasks)
    old = journal.setdefault("intents", {}).get(source)
    intent = {"identity": deepcopy(candidate["identity"]), "hashes": hashes, "state": "pending"}
    if old and (old.get("state") == "removed" or old.get("identity") != intent["identity"] or old.get("hashes") != hashes):
        raise CoordinationError("待清理源或关联种子已变化")
    journal["intents"][source] = intent
    save(deepcopy(journal))

    def recheck_qb():
        current = qb.tasks_for([source], journal.get("tasks", {}))
        now = {task["hash"]: task for task in current}
        if sorted(now) != hashes:
            return False
        return all(now[task["hash"]]["files"] == task["files"] and qb.verify_task(task) for task in tasks)

    if not delete_source(recheck_qb):
        return {"cleaned": False, "reason": "删除前复核未通过", "removed_tasks": 0}

    # 如这里持久化失败，种子仍保持暂停，不尝试删除任务；留给人工核查。
    journal["intents"][source]["state"] = "removed"
    for task in tasks:
        retired = records[task["hash"]]["retired"]
        if source not in retired:
            retired.append(source)
    save(deepcopy(journal))

    removed_tasks = 0
    for task in tasks:
        record = records[task["hash"]]
        if _all_sources_gone(record, source_absent):
            if qb.remove(task):
                record["removed"] = True
                removed_tasks += 1
                save(deepcopy(journal))
    return {"cleaned": True, "reason": "已清理", "removed_tasks": removed_tasks}


def finalize_ready(qb, journal, save, source_absent):
    """重试已确认全部源文件清理完成的任务；任何不确定状态保留任务。"""
    removed = 0
    for tid, record in journal.get("tasks", {}).items():
        if record.get("removed") or not record.get("files"):
            continue
        task = {"hash": tid, "files": record["files"]}
        if not _all_sources_gone(record, source_absent):
            continue
        actual = qb.get_task(tid)
        if actual is not None:
            if actual["files"] != task["files"]:
                raise CoordinationError("待删除种子的文件清单发生变化")
            if not qb.remove(actual):
                continue
        record["removed"] = True
        removed += 1
        save(deepcopy(journal))
    return removed
