"""D1 trajectory flywheel - structured execution trajectory persistence.

Persists cowork DAG workflow Hook events as structured records (not just text logs),
fills sessions.steps_snapshot, and writes to the unified aggregation directory ~/.fusion/trajectories/cowork/.

Integration: register hook handlers via TrajectoryRecorder.attach(hook_manager);
persistence happens automatically on HookManager.fire().
"""

from .recorder import TrajectoryRecorder, TrajectoryWriter
from .space import SpaceMessageTrajectory, SpaceTrajectoryExporter

__all__ = [
    "TrajectoryRecorder",
    "TrajectoryWriter",
    "SpaceTrajectoryExporter",
    "SpaceMessageTrajectory",
]
