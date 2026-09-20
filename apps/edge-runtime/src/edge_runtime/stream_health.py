"""流健康事实的生产/解码契约, 也是 `vendor/` 健康 hook 唯一允许调用的模块.

基座 pipeline 回调只登记真实可观测的 source error、delivering 与 EOS。vendor 在动作
进入 `_chunk_queue` 前用 stream epoch barrier 退休尚未完成 normalization 的旧 work;
DDM metadata producer 同步携带代际, EOS 在尾部 chunk flush 后投递, 活跃 VLM wait 会被
代际变化唤醒。健康事实随后沿既有 chunk/VLM/SSE 链输出。

生产者运行在基座容器, 消费者运行在 supervisor, 二者可独立升级, 因此未知事实必须
按原始值保留 (ADR-0003), 且无法分类的事实按观测受损处理而不是按健康处理.

本模块只依赖标准库, 也不导入 `edge_runtime` 的其他模块; barrier 与队列所有权仍在
vendor 基座本地, 避免健康 hook 扩大到判定实现.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any, Protocol

STREAM_HEALTH_KEY = "stream_health"
"""The key that marks a synthetic chunk as ours.

A key rather than a sentinel value: a consumer branches on whether the key is present,
which is safer than recognizing a magic number and does not collide with the base's own
`chunk_idx=-1` end-of-stream chunk (§5.11).
"""

DETAIL_LIMIT = 200
"""How much of the base's message text rides along. It is triage detail on one SSE frame,
not a payload, and its length is not ours to trust."""


class StreamFact(Enum):
    """由拥有该接缝的组件直接观测到的流有效性事实.

    pipeline hook 产生 `SOURCE_ERROR`、`DELIVERING`、`STREAM_ENDED`; 本机 SSE 输入看护
    还可根据网络读取与队列状态产生 `INFERENCE_TIMEOUT`、`CHUNK_BACKLOG_EXCEEDED`.

    不定义 `RECONNECTING`: DeepStream 在源元件内部重试但不广播该状态, 恢复由
    `SOURCE_ERROR` 后的 `DELIVERING` 表达. 时间轴归零也不是独立事实; pipeline 事件与
    动作 chunk 在可用时携带 `source_anchor`, supervisor 通过锚点变化识别归零.
    """

    SOURCE_ERROR = "source_error"
    DELIVERING = "delivering"
    STREAM_ENDED = "stream_ended"
    INFERENCE_TIMEOUT = "inference_timeout"
    CHUNK_BACKLOG_EXCEEDED = "chunk_backlog_exceeded"

    @property
    def impairs_observation(self) -> bool:
        """Whether this fact says the observation window was not usable.

        Classification lives with the fact rather than in the supervisor, so a member
        cannot be added without deciding what it means for judgment — the same reason
        `ReasonCode` carries its own verdict class.
        """
        return self is not StreamFact.DELIVERING


class HealthSink(Protocol):
    """健康事实写入点的最小契约, 只要求 FIFO 队列提供 `put`.

    vendor hook 实际写入动作共用的 `_chunk_queue`; VLM 启用时基座按 `stream_health`
    键旁路推理并继续沿 future/response 队列转发, VLM 禁用时该队列直接进入 SSE.
    """

    def put(self, item: dict[str, Any], /) -> None: ...


@dataclass(frozen=True, slots=True)
class StreamHealthEvent:
    """One statement about whether a stream could be observed.

    Not an observation: an observation states what happened at the station, this states
    whether we could see it at all (CONTEXT.md). It therefore never enters the missing-step
    comparison and never reaches the observation storage or reporting path.
    """

    fact: StreamFact | str
    """The fact, or the raw wire string when this build does not know it.

    A `str` here means the producer is newer than this consumer. Use
    `isinstance(event.fact, StreamFact)` to tell them apart; `impairs_observation` already
    reads an unknown fact conservatively, so most callers do not need to.
    """

    at_monotonic: float | None
    """事实产生接缝观测到它时的主机 monotonic 时间.

    判定核心的 idle timeout 与 step deadline 使用同一时钟. 跨进程可比较依赖 Linux
    `CLOCK_MONOTONIC` 从 boot 起计时这一前提; 该前提及容器 time namespace 风险记录在
    measured-facts.md §2.13. 格式错误事件可为 None, 此时本身就表示观测不可信.
    """

    source_anchor: float | None = None
    """基座 `first_timestamp`, 即源时间轴建立锚点时的 wall-clock.

    它来自 `time.time()`, 只用于比较身份, 不能用于测量间隔. 登记的 vendor 补丁在当前
    chunk 算法观测到 PTS 回退时重新锚定, 下一正常 chunk 携带新值供 supervisor 识别归零.
    """

    stream_id: str = ""
    detail: str = ""

    @property
    def impairs_observation(self) -> bool:
        """Whether this event says the observation window was not usable."""
        if isinstance(self.fact, StreamFact):
            return self.fact.impairs_observation
        # A fact this build cannot classify. Conservative rather than optimistic (§5.21):
        # reading it as healthy would let an unobservable pass close as passing.
        return True

    def as_chunk(self) -> dict[str, Any]:
        """The synthetic chunk that goes on the queue, carrying only our own key."""
        fact = self.fact.value if isinstance(self.fact, StreamFact) else self.fact
        return {
            STREAM_HEALTH_KEY: {
                "fact": fact,
                "stream_id": self.stream_id,
                "at_monotonic": self.at_monotonic,
                "source_anchor": self.source_anchor,
                "detail": self.detail,
            }
        }


def note_pipeline_message(
    message: object,
    *,
    sink: HealthSink,
    stream_id: str,
    source_anchor: float,
    clock: Callable[[], float] = monotonic,
) -> StreamHealthEvent | None:
    """The hook's entry point: turn one pipeline message into an event, or into nothing.

    Called from the base's `on_message` after its own branches, once per message. Returns
    the event that was queued, or None when the message says nothing about health — the
    base's pre-roll state transitions are most messages, and reporting them would mark a
    healthy stream impaired every time a pipeline starts.

    The message is duck-typed. Importing `pyservicemaker` to name its classes would tie
    this module to the DeepStream container and make it untestable on a bare CPU, and what
    the hook depends on is the attributes rather than the class identities. That dependency
    is pinned from the other side, by the contract suite asserting the base still imports
    those message types (§5.9).
    """
    fact = _classify(message)
    if fact is None:
        return None
    event = StreamHealthEvent(
        fact=fact,
        at_monotonic=clock(),
        source_anchor=source_anchor if source_anchor > 0 else None,
        stream_id=stream_id,
        detail=_detail(message),
    )
    sink.put(event.as_chunk())
    return event


def _classify(message: object) -> StreamFact | None:
    """Which fact this message carries, if any.

    Read off the message's shape rather than its type: a state transition carries
    `new_state`, and end-of-stream carries nothing but its name. The base's own callback
    branches on `isinstance` against the classes it imported, which this module cannot do
    without importing the container's world.
    """
    state = getattr(message, "new_state", None)
    if state is not None:
        return _STATE_FACTS.get(getattr(state, "name", ""))

    name = type(message).__name__
    if "EOS" in name:
        return StreamFact.STREAM_ENDED
    if "Error" in name or getattr(message, "error", None) is not None:
        return StreamFact.SOURCE_ERROR
    return None


_STATE_FACTS = {
    # `INVALID` is how the base's own callback learns the pipeline failed: it sets
    # `_started_event` there so a caller waiting to start is released (`ds_sop_process.py`).
    "INVALID": StreamFact.SOURCE_ERROR,
    "PLAYING": StreamFact.DELIVERING,
}
"""Pipeline states that are health facts. `READY`, `PAUSED` and `NULL` are the base's
start-up and teardown bookkeeping, and are not."""


def _detail(message: object) -> str:
    """The base's message text, bounded, and never at the cost of the event.

    A message whose `__str__` raises must still produce its fact: losing the event means
    the supervisor concludes on a stream it could not see, which is the one outcome §5.2
    forbids. The detail is triage convenience, so it is what gets dropped.
    """
    try:
        return str(message)[:DETAIL_LIMIT]
    except Exception:
        return ""


def decode(chunk: Mapping[str, Any]) -> StreamHealthEvent | None:
    """Read one SSE chunk as a health event, or None when it is an ordinary chunk.

    The supervisor calls this on everything the stream delivers, so a real chunk of work
    must come back as "not one of ours" rather than as a malformed event. A chunk that
    *is* ours but does not parse comes back as a source error: producer and consumer
    disagreeing is itself a reason to distrust what we are seeing, and raising here would
    put a decoding failure into the supervisor's event loop.
    """
    if STREAM_HEALTH_KEY not in chunk:
        return None
    payload = chunk[STREAM_HEALTH_KEY]
    if not isinstance(payload, Mapping):
        return StreamHealthEvent(fact=StreamFact.SOURCE_ERROR, at_monotonic=None)
    source_anchor = _number(payload.get("source_anchor"))
    return StreamHealthEvent(
        fact=_decode_fact(payload.get("fact")),
        at_monotonic=_number(payload.get("at_monotonic")),
        source_anchor=source_anchor if source_anchor is not None and source_anchor > 0 else None,
        stream_id=str(payload.get("stream_id", "")),
        detail=str(payload.get("detail", ""))[:DETAIL_LIMIT],
    )


def _decode_fact(raw: object) -> StreamFact | str:
    """A known fact as its member, an unknown one as itself (ADR-0003)."""
    if not isinstance(raw, str) or not raw:
        return StreamFact.SOURCE_ERROR
    try:
        return StreamFact(raw)
    except ValueError:
        return raw


def _number(raw: object) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
