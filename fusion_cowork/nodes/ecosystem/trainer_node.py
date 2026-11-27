"""Fusion ecosystem interop node — bridges to the fusion-trainer CLI in the same-repo .venv.

Offline hard constraint: invokes the local fusion-trainer as a subprocess only,
imports no training libraries (mlx-lm etc.), honoring the "may only integrate
fusion-mlx / external CLIs" constraint.
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

# Only expose training methods fusion-trainer currently implements.
# dpo/orpo are blocked by upstream fusion-mlx#399 — not exposed here to avoid misleading users.
_SUPPORTED_METHODS = frozenset({"sft", "grpo"})


def _resolve_trainer_bin() -> str:
    # E-1: no hardcoded /Users/dahai paths. Env override first, then PATH lookup, else empty string (clear error at runtime).
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
    """fusion-trainer fine-tune node — feeds datasets into the local fusion-trainer CLI for fine-tuning.

    Wires at the end of a workflow DAG: the JSONL dataset produced upstream can be
    fine-tuned in place, forming the *workflow output → fine-tune → model upgrade*
    local loop (D1 trajectory flywheel).
    """

    name = "fusion_trainer"
    display_name = "模型微调"  # i18n: user-facing
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "调用本地 fusion-trainer CLI 对数据集做 SFT/GRPO 微调"  # i18n: user-facing
    icon = "🎓"
    default_label = "模型微调"  # i18n: user-facing

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
                # i18n: user-facing
                error="缺少必填参数 dataset 或 model",
                # i18n: user-facing
                summary="参数不完整",
            )

        if method not in _SUPPORTED_METHODS:
            logger.warning(f"TrainerNode unsupported method={method} (implemented: sft/grpo)")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=(
                    f"不支持的训练方法 '{method}'；仅支持 {sorted(_SUPPORTED_METHODS)}"  # i18n: user-facing
                    "（dpo/orpo 由上游 fusion-mlx#399 阻塞）"  # i18n: user-facing
                ),
                # i18n: user-facing
                summary="method 不支持",
            )

        bin_path = _resolve_trainer_bin()
        if not bin_path or not Path(bin_path).exists():
            logger.error(f"fusion-trainer CLI not found: {bin_path!r}")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=(
                    f"fusion-trainer CLI 未找到: {bin_path!r}"  # i18n: user-facing
                    "（请在 PATH 安装 fusion-trainer, 或 export FUSION_TRAINER_BIN=<绝对路径>）"  # i18n: user-facing
                ),
                # i18n: user-facing
                summary="CLI 缺失",
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
                    error=f"微调执行超时 ({timeout}s)",  # i18n: user-facing
                    data={"command": command, "timeout": timeout},
                    summary="执行超时",  # i18n: user-facing
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
                error=None if return_code == 0 else f"fusion-trainer 退出码 {return_code}",  # i18n: user-facing
                # i18n: user-facing
                summary=(f"微调{'成功' if return_code == 0 else '失败'} (method={method}, 返回码={return_code})"),
            )

        except Exception as e:
            logger.exception("TrainerNode execution exception")
            return NodeResult(
                status=NodeStatus.FAILED,
                # i18n: user-facing
                error=f"微调执行异常: {e}",
                # i18n: user-facing
                summary="执行异常",
            )
