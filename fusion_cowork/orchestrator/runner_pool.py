"""Mailbox consumer pool (issue #190) — daemon-resident message consumer.

Every agent registered in the roster (cowork_roster) is a live consumption line:
fetch unread -> dispatch to the handler (run the workflow) -> ack only after dispatch succeeds (at-least-once).
After the run reaches a terminal state the result is mailed back to the sender mailbox — cross-repo conversation with no manual RPC pulling.

Drive modes:
- Push: a local TeamMailbox.send triggers wake() for immediate consumption; external parties (other daemons)
  can call wake() / HTTP notify, see /api/team/notify in web_api.
- Polling: fallback interval FUSION_INBOX_POLL_INTERVAL (default 5s), covering peers without notify capability.

issue #494 (K4) kernel convergence note, RE-RATIFIED by issue #533 item 3
(2026-11-19, against the kernel-abstract-roadmap-2026-11-14.md ruling): kernel
TeamRuntime.run_member is NOT the execution path for this pool — and that
is the ratified end state, not a pending gap. run_member's mechanism is
LLM subagent execution (kernel SubagentRunner: SpawnBrief contract, member
prompt assembly, depth<=3 stacking, run streams); it is adopted where that
mechanism belongs — the space kernel plane (issue #496) and teams.run_member
(nodes/ecosystem/team_node.py). This pool's execution path is workflow
dispatch: dispatch() launches a cowork WorkflowEngine run per rostered
agent and replies through the cross-repo mailbox protocol (at-least-once
ack, in-flight dedup, terminal-state reply collection). The roadmap's own
ruling keeps DAG composition in the cowork WorkflowEngine as the repo-wide
single DAG layer — wrapping workflow dispatch inside a kernel subagent
session would relocate product semantics into a mechanism shell while the
durable consumer semantics (ack-after-dispatch, crash replay) stay
stricter than the kernel inbox model. Revisit only if the kernel gains a
workflow-dispatch member runner (tracked here).
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
    """Roster-driven durable message consumer pool."""

    def __init__(
        self,
        roster: Any,
        mailbox: Any,
        dispatch: Callable[[Dict[str, Any], Dict[str, Any]], Any],
        get_run: Optional[Callable[[str], Any]] = None,
        poll_interval: float = 5.0,
        reply: bool = True,
    ):
        # dispatch(agent, message) -> run_id (async): dispatch failure must raise, no ack
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
        # message_id -> in-flight run, prevents the same message being dispatched again before the run is terminal
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
        # local send wakes immediately (in-process leg of the push path)
        try:
            self.mailbox.on_send = self.wake
        except Exception as e:
            logger.warning(f"Failed to register mailbox push hook (falling back to pure polling): {e}")

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
            f"Mailbox consumer pool started: poll_interval={self.poll_interval}s agents={len(self.roster.list(enabled_only=True))}"
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
        logger.info("Mailbox consumer pool stopped")

    async def _loop(self) -> None:
        while self._running:
            # clear before tick: send/wake arriving during tick keep the set set,
            # so the next wait returns immediately — the old order (clear after tick) swallowed that wakeup,
            # so messages waited a full poll_interval before consumption (exposed on slow macOS CI machines).
            self._wake_event.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Mailbox consumer pool tick failed: {e}")
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
                logger.error(f"Consumer pool failed to read mailbox agent={agent_id}: {e}")
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
            # no ack: redelivered next tick (at-least-once)
            self.metrics["dispatch_errors"] += 1
            logger.error(f"Consumer pool dispatch failed agent={agent_id} message={message_id}: {e}")
            return
        if not run_id:
            self.metrics["dispatch_errors"] += 1
            logger.error(f"Consumer pool dispatch returned no run_id agent={agent_id} message={message_id}")
            return
        self._in_flight[message_id] = run_id
        acked = await asyncio.to_thread(self.mailbox.ack, agent_id, message_id)
        if not acked:
            logger.warning(f"Consumer pool ack miss agent={agent_id} message={message_id} (peer already acked?)")
        self.metrics["consumed"] += 1
        per = self.metrics["per_agent"].setdefault(agent_id, {"consumed": 0, "replies_sent": 0, "errors": 0})
        per["consumed"] += 1
        latency_ms = (time.time() - started) * 1000
        logger.info(
            f"Consumer pool dispatched agent={agent_id} message={message_id} run={run_id} "
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
                logger.warning(f"Consumer pool failed to query run run={run_id}: {e}")
                continue
            if not run:
                continue
            status = str(run.get("status", "")).lower()
            if status not in _TERMINAL:
                continue
            self._in_flight.pop(info["message_id"], None)
            self._pending_replies.pop(run_id, None)
            if status not in ("completed", "success"):
                logger.warning(f"Consumer pool run not successful run={run_id} status={status} (no reply)")
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
                logger.error(f"Consumer pool reply failed run={run_id} -> {info['recipient']}: {e}")
                continue
            self.metrics["replies_sent"] += 1
            per = self.metrics["per_agent"].setdefault(
                info["agent_id"], {"consumed": 0, "replies_sent": 0, "errors": 0}
            )
            per["replies_sent"] += 1
            logger.info(f"Consumer pool replied {info['agent_id']} -> {info['recipient']} run={run_id}")

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
            logger.warning(f"Consumer pool metrics refresh failed (ignored): {e}")
        logger.debug(
            f"Consumer pool metrics: depth={self.metrics['mailbox_depth']} "
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
            logger.warning(f"Consumer pool status failed to query roster: {e}")
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
    # The trio: mailbox depth / oldest unacked age / consumed count — attached to the existing OTel surface
    try:
        from ..observability.metrics import get_meter

        meter = get_meter()
        if "depth" not in _instruments:
            _instruments["depth"] = meter.create_histogram(
                "fusion_team_mailbox_depth", description="Team mailbox backlog depth"
            )
            _instruments["oldest"] = meter.create_histogram(
                "fusion_team_oldest_unacked_age_seconds", description="Age of the oldest unacked message"
            )
            _instruments["consumed"] = meter.create_counter(
                "fusion_team_mailbox_consumed_total", description="Total consumed messages"
            )
            _instruments["replies"] = meter.create_counter(
                "fusion_team_mailbox_replies_total", description="Total replies sent"
            )
        _instruments["depth"].record(metrics["mailbox_depth"])
        _instruments["oldest"].record(metrics["oldest_unacked_age_s"])
        # counters only add increments; replaying cumulative values would double-count
        for key, instrument in (("consumed", "consumed"), ("replies_sent", "replies")):
            delta = int(metrics.get(key, 0)) - _last_exported[key]
            if delta > 0:
                _instruments[instrument].add(delta)
                _last_exported[key] = int(metrics.get(key, 0))
    except Exception as e:
        logger.debug(f"Consumer pool metrics export skipped: {e}")
