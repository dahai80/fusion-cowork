"""Benchmark — Claude Cowork vs Fusion-Desk 功能对比与性能度量。"""

from .matrix import CapabilityMatrix, Capability, CapabilityLevel
from .runner import BenchmarkRunner, BenchmarkResult
from .report import ReportRenderer

__all__ = [
    "CapabilityMatrix", "Capability", "CapabilityLevel",
    "BenchmarkRunner", "BenchmarkResult",
    "ReportRenderer",
]
