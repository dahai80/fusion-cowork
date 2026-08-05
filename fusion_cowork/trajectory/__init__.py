"""D1 轨迹飞轮 — 结构化执行轨迹持久化。

将 cowork DAG 工作流的 Hook 事件落库为结构化记录（非仅文本日志），
填充 sessions.steps_snapshot，并写入统一汇聚目录 ~/.fusion/trajectories/cowork/。

接入方式：通过 TrajectoryRecorder.attach(hook_manager) 注册 Hook 处理器，
随 HookManager.fire() 自动持久化。
"""

from .recorder import TrajectoryEvent, TrajectoryRecorder, TrajectoryWriter
from .space import SpaceMessageTrajectory, SpaceTrajectoryExporter

__all__ = [
    "TrajectoryRecorder",
    "TrajectoryEvent",
    "TrajectoryWriter",
    "SpaceTrajectoryExporter",
    "SpaceMessageTrajectory",
]
