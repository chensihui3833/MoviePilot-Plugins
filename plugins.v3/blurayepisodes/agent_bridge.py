"""Scoped, cancellable bridge from the synchronous provider to MoviePilot Agent."""
import asyncio
import copy
import json
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout

from .core import NeedsReview


class MappingBroker:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def open(self):
        job = uuid.uuid4().hex
        prefix = "__bluray_mapping_" + job
        with self._lock:
            self._jobs[job] = {"prefix": prefix, "mapping": None}
        return job, prefix

    def submit(self, job, session, mapping):
        encoded = json.dumps(mapping, ensure_ascii=False)
        if len(encoded) > 1024 * 1024:
            raise NeedsReview("配对结果超过大小限制")
        with self._lock:
            record = self._jobs.get(job)
            if not record or not session.startswith(record["prefix"] + "_"):
                raise NeedsReview("任务不存在、已取消或不属于当前助手会话")
            if record["mapping"] is not None:
                raise NeedsReview("此任务已经提交结果，不能覆盖")
            record["mapping"] = copy.deepcopy(mapping)

    def take(self, job):
        with self._lock:
            record = self._jobs.get(job)
            return copy.deepcopy(record["mapping"]) if record else None

    def close(self, job):
        with self._lock:
            self._jobs.pop(job, None)


broker = MappingBroker()


def wait_for_analysis(future, timeout, should_stop=None):
    end = time.monotonic() + timeout
    while True:
        if should_stop and should_stop():
            future.cancel()
            raise NeedsReview("整理已停止，已取消助手分析等待")
        remaining = end - time.monotonic()
        if remaining <= 0:
            future.cancel()
            raise NeedsReview("助手分集分析超时，请查看插件状态后重试")
        try:
            return future.result(timeout=min(1, remaining))
        except FutureTimeout:
            if future.done():
                raise
            continue


def analyze(source, season, timeout=1200, should_stop=None):
    # Internal integration intentionally version-gated by the source manifest.
    from app.application.agent import get_running_agent_manager
    from app.runtime.loop import main_loop_registry
    manager = get_running_agent_manager()
    if manager is None or not callable(getattr(manager, "run_background_prompt", None)):
        raise NeedsReview("MoviePilot 智能助手未启用、未配置或后台接口不可用")
    loop = main_loop_registry.require()
    if not loop.is_running():
        raise NeedsReview("MoviePilot 后台事件循环未运行")
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        raise NeedsReview("当前整理入口运行在事件循环线程，不能同步等待助手")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise NeedsReview("助手分析超时设置无效")
    if not 30 <= timeout <= 3600:
        raise NeedsReview("助手分析超时须在 30–3600 秒之间")
    job, prefix = broker.open()
    message = (
        f"这是电视剧第 {season} 季的原盘下载目录：\n{source}\n"
        "请识别里面每一集对应的视频文件、播放列表和集名，只分析，不修改文件。"
        f"完成后用 submit_bluray_mapping 工具提交结果，job_id={job}；不能确认的关系填入 unknown。"
    )
    future = None
    try:
        future = asyncio.run_coroutine_threadsafe(
            manager.run_background_prompt(message=message, session_prefix=prefix,
                                          allow_message_tools=False), loop)
        wait_for_analysis(future, timeout, should_stop)
        mapping = broker.take(job)
        if mapping is None:
            raise NeedsReview("助手未通过 submit_bluray_mapping 提交可核对结果；保留原盘并停止")
        return mapping
    except NeedsReview:
        raise
    except Exception as exc:
        raise NeedsReview(f"助手分析执行失败：{type(exc).__name__}: {exc}") from exc
    finally:
        if future is not None and not future.done():
            future.cancel()
        broker.close(job)
