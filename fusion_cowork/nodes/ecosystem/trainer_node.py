"""Fusion ecosystem interop node — bridges to the fusion-trainer CLI in the in-repo .venv.

Offline hard constraint: invokes local fusion-trainer only as a subprocess, never
imports any training library (mlx-lm etc.), honoring the "integrate only with
fusion-mlx / external CLI" constraint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeResult,
    NodeStatus,
    coerce_params,
    register_node,
)

logger = logging.getLogger(__name__)

# Expose only training methods fusion-trainer currently implements.
# dpo/orpo is blocked upstream by fusion-mlx#399 — not exposed here to avoid misleading users.
_SUPPORTED_METHODS = frozenset({"sft", "grpo"})


def _resolve_trainer_bin() -> str:
    # E-1: remove the hardcoded /Users/dahai path. Env override first, then PATH lookup, finally empty string (clear runtime error).
    bin_path = os.environ.get("FUSION_TRAINER_BIN", "").strip()
    if bin_path:
        return bin_path
    found = shutil.which("fusion-trainer")
    if found:
        logger.debug(f"fusion-trainer resolved from PATH: {found}")
        return found
    return ""


def _build_sft_args(dataset: str, model: str, config: str, output_dir: str) -> List[str]:
    args = ["sft", "--dataset", dataset, "--model", model]
    if config:
        args += ["--config", config]
    if output_dir:
        args += ["--output", output_dir]
    return args


def _build_rlsl_args(method: str, dataset: str, model: str, config: str) -> List[str]:
    args = ["rlsl", "--method", method, "--dataset", dataset, "--model", model]
    if config:
        args += ["--config", config]
    return args


@register_node
class TrainerNode(BaseNode):
    """fusion-trainer fine-tune node — sends datasets to the local fusion-trainer CLI for fine-tuning.

    Wire this node at the end of a workflow DAG to fine-tune the JSONL dataset
    produced upstream in place, forming a local closed loop of
    *workflow output -> fine-tune -> model upgrade* (D1 trajectory flywheel).
    """

    name = "fusion_trainer"
    # i18n: user-facing
    display_name = "模型微调"
    category = NodeCategory.FUSION_ECOSYSTEM
    # i18n: user-facing
    description = "调用本地 fusion-trainer CLI 对数据集做 SFT/GRPO 微调"
    icon = "🎓"
    # i18n: user-facing
    default_label = "模型微调"

    inputs = [
        # i18n: user-facing
        {"key": "dataset", "label": "数据集路径", "type": "string"},
        # i18n: user-facing
        {"key": "model", "label": "基座模型", "type": "string"},
    ]
    outputs = [
        # i18n: user-facing
        {"key": "return_code", "label": "返回码", "type": "integer"},
        # i18n: user-facing
        {"key": "stdout", "label": "标准输出", "type": "string"},
        # i18n: user-facing
        {"key": "stderr", "label": "错误输出", "type": "string"},
        # i18n: user-facing
        {"key": "command", "label": "实际命令", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "JSONL 数据集路径，或 'hub:category/name'",
                },
                "model": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "基座模型 id（如 qwen2.5-7b-4bit）",
                },
                "method": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "训练方法: sft（默认）| grpo",
                    "default": "sft",
                },
                "config": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "训练配置 YAML 路径（可选）",
                    "default": "",
                },
                "output_dir": {
                    "type": "string",
                    # i18n: user-facing
                    "description": "输出目录（仅 sft，可选）",
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    # i18n: user-facing
                    "description": "超时秒数（0=不超时）",
                    "default": 0,
                },
            },
            "required": ["dataset", "model"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        params = self.config.params
        schema = self.get_params_schema()
        params = coerce_params(params, schema)

        dataset = inputs.get("dataset", params.get("dataset", ""))
        model = inputs.get("model", params.get("model", ""))
        method = inputs.get("method", params.get("method", "sft")) or "sft"
        config = inputs.get("config", params.get("config", "")) or ""
        output_dir = inputs.get("output_dir", params.get("output_dir", "")) or ""
        timeout = params.get("timeout", 0) or 0

        if not dataset or not model:
            return NodeResult(
                status=NodeStatus.FAILED,
                error="missing required params dataset or model",
                summary="incomplete params",
            )

        if method not in _SUPPORTED_METHODS:
            logger.warning(f"TrainerNode unsupported method={method} (implemented: sft/grpo)")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"unsupported training method '{method}'; only {sorted(_SUPPORTED_METHODS)} supported"
                    " (dpo/orpo blocked upstream by fusion-mlx#399)"
                ),
                summary="unsupported method",
            )

        bin_path = _resolve_trainer_bin()
        if not bin_path or not Path(bin_path).exists():
            logger.error(f"fusion-trainer CLI not found: {bin_path!r}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"fusion-trainer CLI not found: {bin_path!r}"
                    " (install fusion-trainer on PATH, or export FUSION_TRAINER_BIN=<absolute path>)"
                ),
                summary="CLI missing",
            )

        if method == "sft":
            args = _build_sft_args(dataset, model, config, output_dir)
        else:
            args = _build_rlsl_args(method, dataset, model, config)

        command = [bin_path] + args
        logger.info(f"TrainerNode spawn: {' '.join(command)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                if timeout and timeout > 0:
                    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                else:
                    stdout_b, stderr_b = await proc.communicate()
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"fine-tune execution timed out ({timeout}s)",
                    data={"command": command, "timeout": timeout},
                    summary="execution timed out",
                )

            stdout_str = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr_str = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
            return_code = proc.returncode if proc.returncode is not None else -1

            status = NodeStatus.SUCCESS if return_code == 0 else NodeStatus.FAILED
            return NodeResult(
                status=status,
                data={
                    "return_code": return_code,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "command": command,
                    "method": method,
                    "dataset": dataset,
                    "model": model,
                },
                error=None if return_code == 0 else f"fusion-trainer exit code {return_code}",
                summary=(f"fine-tune {'succeeded' if return_code == 0 else 'failed'} (method={method}, return_code={return_code})"),
            )

        except Exception as e:
            logger.exception("TrainerNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"fine-tune execution exception: {e}",
                summary="execution exception",
            )
