"""邮箱消费者池 (issue #190) — daemon 常驻的消息消费端。

花名册 (cowork_roster) 里登记的每个 agent 都是一条活消费线:
拉取未读 → 派发给 handler (跑工作流) → 派发成功才 ack (at-least-once)。
run 终态后把结果作为回信投回发送方信箱, 跨仓对话无需任何人工 RPC 拉动。

推进方式:
- 推送: 本地 TeamMailbox.send 触发 wake() 立即消费; 外部 (其他 daemon)
  可调 wake() / HTTP notify 唤醒, 见 web_api 的 /api/team/notify。
- 轮询: 兜底间隔 FUSION_INBOX_POLL_INTERVAL (默认 5s), 覆盖无通知能力的对端。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_TERMINAL = {"completed", "success", "failed", "canceled", "cancelled", "timeout"}
_REPLY_BODY_MAX = 4000


class RunnerPool:
    """花名册驱动的 durable 消息消费池。"""

    def __init__(
        self,
        roster: Any,
        mailbox: Any,
        dispatch: Callable[[Dict[str, Any], Dict[str, Any]], Any],
        get_run: Optional[Callable[[str], Any]] = None,
        poll_interval: float = 5.0,
        reply: bool = True,
    ):
        # dispatch(agent, message) -> run_id (async): 派发失败必须抛异常, 不 ack
        # get_run(run_id) -> {status, last_result, last_error} | None (async)
        self.roster = roster
        self.mailbox = mailbox
        self.dispatch = dispatch
        self.get_run = get_run
        self.poll_interval = poll_interval
        self.reply = reply
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._wake_event = asyncio.Event()
        # message_id → 在途 run, 防止同一消息在 run 未终态前被重复派发
        self._in_flight: Dict[str, str] = {}
        self._pending_replies: Dict[str, Dict[str, Any]] = {}
        self.metrics: Dict[str, Any] = {
            "consumed": 0,
            "replies_sent": 0,
            "dispatch_errors": 0,
            "mailbox_depth": 0,
            "oldest_unacked_age_s": 0.0,
            "per_agent": {},
        }
        # 本地发送即刻唤醒 (推送路径的进程内一段)
        try:
            self.mailbox.on_send = self.wake
        except Exception as e:
            logger.warning(f"邮箱推送钩子注册失败 (退回纯轮询): {e}")

    @property
    def running(self) -> bool:
        return self._running

    def wake(self, *_args: Any) -> None:
        self._wake_event.set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            f"邮箱消费池启动: poll_interval={self.poll_interval}s agents={len(self.roster.list(enabled_only=True))}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("邮箱消费池已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"邮箱消费池 tick 失败: {e}")
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue

    async def tick(self) -> None:
        await self._dispatch_new_messages()
        await self._collect_replies()
        self._refresh_metrics()

    async def _dispatch_new_messages(self) -> None:
        agents = await asyncio.to_thread(self.roster.list, True)
        for agent in agents:
            agent_id = agent["agent_id"]
            try:
                messages = await asyncio.to_thread(self.mailbox.inbox, agent_id)
            except Exception as e:
                self.metrics["dispatch_errors"] += 1
                logger.error(f"消费池读取信箱失败 agent={agent_id}: {e}")
                continue
            for message in messages:
                await self._dispatch_one(agent, message)

    async def _dispatch_one(self, agent: Dict[str, Any], message: Dict[str, Any]) -> None:
        agent_id = agent["agent_id"]
        message_id = message["message_id"]
        if message_id in self._in_flight:
            return
        started = time.time()
        try:
            run_id = await self.dispatch(agent, message)
        except Exception as e:
            # 不 ack: 下一 tick 重投 (at-least-once)
            self.metrics["dispatch_errors"] += 1
            logger.error(f"消费池派发失败 agent={agent_id} message={message_id}: {e}")
            return
        if not run_id:
            self.metrics["dispatch_errors"] += 1
            logger.error(f"消费池派发未拿到 run_id agent={agent_id} message={message_id}")
            return
        self._in_flight[message_id] = run_id
        acked = await asyncio.to_thread(self.mailbox.ack, agent_id, message_id)
        if not acked:
            logger.warning(f"消费池 ack 未命中 agent={agent_id} message={message_id} (对端已 ack?)")
        self.metrics["consumed"] += 1
        per = self.metrics["per_agent"].setdefault(agent_id, {"consumed": 0, "replies_sent": 0, "errors": 0})
        per["consumed"] += 1
        latency_ms = (time.time() - started) * 1000
        logger.info(
            f"消费池已派发 agent={agent_id} message={message_id} run={run_id} "
            f"sender={message.get('sender', '')} latency={latency_ms:.0f}ms"
        )
        if self.reply and message.get("sender"):
            self._pending_replies[run_id] = {
                "agent_id": agent_id,
                "message_id": message_id,
                "recipient": message["sender"],
                "created_at": time.time(),
            }

    async def _collect_replies(self) -> None:
        if not self._pending_replies or self.get_run is None:
            return
        for run_id, info in list(self._pending_replies.items()):
            try:
                run = await self.get_run(run_id)
            except Exception as e:
                logger.warning(f"消费池查询 run 失败 run={run_id}: {e}")
                continue
            if not run:
                continue
            status = str(run.get("status", "")).lower()
            if status not in _TERMINAL:
                continue
            self._in_flight.pop(info["message_id"], None)
            self._pending_replies.pop(run_id, None)
            if status not in ("completed", "success"):
                logger.warning(f"消费池 run 未成功 run={run_id} status={status} (不回信)")
                continue
            body = self._reply_body(info["message_id"], run)
            try:
                await asyncio.to_thread(
                    self.mailbox.send,
                    info["agent_id"],
                    info["recipient"],
                    body,
                    "reply",
                    "",
                )
            except Exception as e:
                self.metrics["dispatch_errors"] += 1
                logger.error(f"消费池回信失败 run={run_id} → {info['recipient']}: {e}")
                continue
            self.metrics["replies_sent"] += 1
            per = self.metrics["per_agent"].setdefault(
                info["agent_id"], {"consumed": 0, "replies_sent": 0, "errors": 0}
            )
            per["replies_sent"] += 1
            logger.info(f"消费池已回信 {info['agent_id']} → {info['recipient']} run={run_id}")

    @staticmethod
    def _reply_body(message_id: str, run: Dict[str, Any]) -> str:
        result = run.get("last_result")
        if result is None:
            result = run.get("result")
        text = result if isinstance(result, str) else str(result if result is not None else "")
        return f"[run {message_id}] {text}"[:_REPLY_BODY_MAX]

    def _refresh_metrics(self) -> None:
        try:
            self.metrics["mailbox_depth"] = int(self.mailbox.depth())
            self.metrics["oldest_unacked_age_s"] = round(float(self.mailbox.oldest_unacked_age()), 3)
        except Exception as e:
            logger.warning(f"消费池指标刷新失败 (忽略): {e}")
        logger.debug(
            f"消费池指标: depth={self.metrics['mailbox_depth']} "
            f"oldest_age={self.metrics['oldest_unacked_age_s']}s "
            f"consumed={self.metrics['consumed']} replies={self.metrics['replies_sent']} "
            f"errors={self.metrics['dispatch_errors']}"
        )
        _export_metrics(self.metrics)

    def status(self) -> Dict[str, Any]:
        agents: List[Dict[str, Any]] = []
        try:
            rows = self.roster.list()
        except Exception as e:
            logger.warning(f"消费池状态查询花名册失败: {e}")
            rows = []
        for row in rows:
            agent_id = row["agent_id"]
            try:
                depth = int(self.mailbox.depth(agent_id))
                oldest = round(float(self.mailbox.oldest_unacked_age(agent_id)), 3)
            except Exception:
                depth, oldest = -1, 0.0
            per = self.metrics["per_agent"].get(agent_id, {})
            agents.append(
                {
                    "agent_id": agent_id,
                    "workflow_id": row.get("workflow_id", ""),
                    "team": row.get("team", ""),
                    "enabled": bool(row.get("enabled", 0)),
                    "inbox_depth": depth,
                    "oldest_unacked_age_s": oldest,
                    "consumed": int(per.get("consumed", 0)),
                    "replies_sent": int(per.get("replies_sent", 0)),
                }
            )
        return {
            "running": self._running,
            "poll_interval": self.poll_interval,
            "consumers": len([a for a in agents if a["enabled"]]),
            "agents": agents,
            "metrics": dict(self.metrics),
        }


_instruments: Dict[str, Any] = {}
_last_exported: Dict[str, int] = {"consumed": 0, "replies_sent": 0}


def _export_metrics(metrics: Dict[str, Any]) -> None:
    # 三件套: 信箱深度 / 最老未 ack 年龄 / 已消费数 — 挂到既有 OTel 表面
    try:
        from ..observability.metrics import get_meter

        meter = get_meter()
        if "depth" not in _instruments:
            _instruments["depth"] = meter.create_histogram("fusion_team_mailbox_depth", description="团队信箱积压深度")
            _instruments["oldest"] = meter.create_histogram(
                "fusion_team_oldest_unacked_age_seconds", description="最老未 ack 消息年龄"
            )
            _instruments["consumed"] = meter.create_counter(
                "fusion_team_mailbox_consumed_total", description="已消费消息总数"
            )
            _instruments["replies"] = meter.create_counter(
                "fusion_team_mailbox_replies_total", description="已回信总数"
            )
        _instruments["depth"].record(metrics["mailbox_depth"])
        _instruments["oldest"].record(metrics["oldest_unacked_age_s"])
        # 计数器只加增量, 累计值重放会让计数二次增长
        for key, instrument in (("consumed", "consumed"), ("replies_sent", "replies")):
            delta = int(metrics.get(key, 0)) - _last_exported[key]
            if delta > 0:
                _instruments[instrument].add(delta)
                _last_exported[key] = int(metrics.get(key, 0))
    except Exception as e:
        logger.debug(f"消费池指标导出跳过: {e}")
