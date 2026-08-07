"""Fusion 生态互通节点 — 对接同仓 .venv 中的 fusion-trainer CLI。

离线硬约束：仅以子进程方式调用本地 fusion-trainer，不 import 任何训练库
（mlx-lm 等），符合『只能对接 fusion-mlx / 对外 CLI』的约束。
"""

from __future__ import annotations

import asyncio
import logging
import os
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

# 本地 fusion-trainer CLI 默认路径（共享 .venv）。FUSION_TRAINER_BIN 可覆盖。
_DEFAULT_TRAINER_BIN = "/Users/dahai/fusion/.venv/bin/fusion-trainer"

# 仅暴露 fusion-trainer 当前已实现的训练方法。
# dpo/orpo 由上游 fusion-mlx#399 阻塞，本节点不暴露，避免误导用户。
_SUPPORTED_METHODS = frozenset({"sft", "grpo"})


def _resolve_trainer_bin() -> str:
    bin_path = os.environ.get("FUSION_TRAINER_BIN", "").strip()
    if bin_path:
        return bin_path
    return _DEFAULT_TRAINER_BIN


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
    """fusion-trainer 微调节点 — 把数据集送进本地 fusion-trainer CLI 做微调。

    工作流 DAG 末端接入此节点，可将上游产出的 JSONL 数据集就地微调，
    形成 *工作流产出 → 微调 → 模型升级* 的本地闭环（D1 轨迹飞轮）。
    """

    name = "fusion_trainer"
    display_name = "模型微调"
    category = NodeCategory.FUSION_ECOSYSTEM
    description = "调用本地 fusion-trainer CLI 对数据集做 SFT/GRPO 微调"
    icon = "🎓"
    default_label = "模型微调"

    inputs = [
        {"key": "dataset", "label": "数据集路径", "type": "string"},
        {"key": "model", "label": "基座模型", "type": "string"},
    ]
    outputs = [
        {"key": "return_code", "label": "返回码", "type": "integer"},
        {"key": "stdout", "label": "标准输出", "type": "string"},
        {"key": "stderr", "label": "错误输出", "type": "string"},
        {"key": "command", "label": "实际命令", "type": "list[string]"},
    ]

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "description": "JSONL 数据集路径，或 'hub:category/name'",
                },
                "model": {
                    "type": "string",
                    "description": "基座模型 id（如 qwen2.5-7b-4bit）",
                },
                "method": {
                    "type": "string",
                    "description": "训练方法: sft（默认）| grpo",
                    "default": "sft",
                },
                "config": {
                    "type": "string",
                    "description": "训练配置 YAML 路径（可选）",
                    "default": "",
                },
                "output_dir": {
                    "type": "string",
                    "description": "输出目录（仅 sft，可选）",
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
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
                error="缺少必填参数 dataset 或 model",
                summary="参数不完整",
            )

        if method not in _SUPPORTED_METHODS:
            logger.warning(f"TrainerNode 不支持的 method={method}（已实现: sft/grpo）")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"不支持的训练方法 '{method}'；仅支持 {sorted(_SUPPORTED_METHODS)}"
                    "（dpo/orpo 由上游 fusion-mlx#399 阻塞）"
                ),
                summary="method 不支持",
            )

        bin_path = _resolve_trainer_bin()
        if not Path(bin_path).exists():
            logger.error(f"fusion-trainer CLI 未找到: {bin_path}")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=(
                    f"fusion-trainer CLI 未找到: {bin_path}"
                    "（请安装 fusion-trainer 或设置 FUSION_TRAINER_BIN）"
                ),
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
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                else:
                    stdout_b, stderr_b = await proc.communicate()
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return NodeResult(
                    status=NodeStatus.FAILED,
                    error=f"微调执行超时 ({timeout}s)",
                    data={"command": command, "timeout": timeout},
                    summary="执行超时",
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
                error=None if return_code == 0 else f"fusion-trainer 退出码 {return_code}",
                summary=(
                    f"微调{'成功' if return_code == 0 else '失败'} "
                    f"(method={method}, 返回码={return_code})"
                ),
            )

        except Exception as e:
            logger.exception("TrainerNode 执行异常")
            return NodeResult(
                status=NodeStatus.FAILED,
                error=f"微调执行异常: {e}",
                summary="执行异常",
            )
