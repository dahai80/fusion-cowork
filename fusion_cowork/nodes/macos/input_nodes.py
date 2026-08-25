"""macOS 鼠标键盘控制节点 — Computer Use 核心。

通过 AppleScript/osascript 实现鼠标移动、点击、键盘输入，
可选 pyobjc 提供更精确的像素级控制。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ...engine.node import (
    BaseNode,
    NodeCategory,
    NodeConfig,
    NodeResult,
    NodeStatus,
    register_node,
)

logger = logging.getLogger(__name__)

from . import run_osascript as _run_osascript


def _try_pyobjc_click(x: int, y: int, button: str = "left", click_count: int = 1) -> bool:
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGEventOtherMouseDown,
            kCGEventOtherMouseUp,
            kCGEventRightMouseDown,
            kCGEventRightMouseUp,
            kCGHIDEventTap,
            kCGMouseButtonCenter,
            kCGMouseButtonLeft,
            kCGMouseButtonRight,
            kCGMouseEventClickState,
        )

        btn_map = {
            "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft),
            "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
            "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter),
        }
        down_type, up_type, btn = btn_map.get(button, btn_map["left"])
        point = (x, y)
        for i in range(click_count):
            down = CGEventCreateMouseEvent(None, down_type, point, btn)
            if i > 0:
                down.setValue(click_count, forField=kCGMouseEventClickState)
            CGEventPost(kCGHIDEventTap, down)
            up = CGEventCreateMouseEvent(None, up_type, point, btn)
            if i > 0:
                up.setValue(click_count, forField=kCGMouseEventClickState)
            CGEventPost(kCGHIDEventTap, up)
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.warning(f"pyobjc click failed: {e}")
        return False


def _try_pyobjc_move(x: int, y: int) -> bool:
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )

        event = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.warning(f"pyobjc move failed: {e}")
        return False


def _try_pyobjc_type(text: str) -> bool:
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSourceCreate,
            kCGEventKeyDown,  # noqa: F401
            kCGEventSourceStateHIDSystemState,
            kCGHIDEventTap,
        )

        source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        for ch in text:
            code = ord(ch)
            down = CGEventCreateKeyboardEvent(source, code, True)
            up = CGEventCreateKeyboardEvent(source, code, False)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.warning(f"pyobjc type failed: {e}")
        return False


def _applescript_click(x: int, y: int, button: str = "left", click_count: int = 1) -> Tuple[int, str]:
    click_cmd = "click at"
    if button == "right":
        click_cmd = "right click at"
    elif click_count >= 2:
        click_cmd = "double click at"
    script = f"""
    tell application "System Events"
        {click_cmd} {{{x}, {y}}}
    end tell
    """
    return _run_osascript(script)


def _applescript_move(x: int, y: int) -> Tuple[int, str]:
    # System Events 无原生鼠标移动命令; 尝试 cliclick (Homebrew 常装), 不可用则明确告警
    script = f'do shell script "/usr/bin/env cliclick m:{x},{y}"'
    rc, out = _run_osascript(script)
    if rc != 0:
        logger.warning(
            f"_applescript_move: cliclick 不可用 (rc={rc}, out={out!r}); "
            "无 pyobjc 时鼠标移动需安装 cliclick (brew install cliclick)"
        )
    return rc, out


def _applescript_type(text: str) -> Tuple[int, str]:
    escaped = text.replace('"', '\\"').replace("\\", "\\\\")
    script = f'''
    tell application "System Events"
        keystroke "{escaped}"
    end tell
    '''
    return _run_osascript(script)


def _applescript_key_code(key_code: int, modifiers: List[str] = None) -> Tuple[int, str]:
    mod_str = ""
    if modifiers:
        mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
    script = f"""
    tell application "System Events"
        key code {key_code}{mod_str}
    end tell
    """
    return _run_osascript(script)


def _applescript_keystroke(key: str, modifiers: List[str] = None) -> Tuple[int, str]:
    mod_str = ""
    if modifiers:
        mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
    script = f'''
    tell application "System Events"
        keystroke "{key}"{mod_str}
    end tell
    '''
    return _run_osascript(script)


def _screenshot_to_data_url(path: str) -> str:
    # 读截图文件并编码为 data URL, 供 OpenAI vision 格式传给大模型
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "image/png" if ext in ("png", "") else f"image/{ext}"
    return f"data:{mime};base64,{b64}"


def _build_vision_user_content(text: str, image_path: str) -> list:
    # 构造 OpenAI 多模态 content: 文本 + 截图 data URL
    # FusionMLXClient.chat 透传 messages 到 /v1/chat/completions, MLX 后端支持则见图像, 不支持则见文本
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": _screenshot_to_data_url(image_path)}},
    ]


_KEY_MAP = {
    "enter": 36,
    "return": 36,
    "tab": 48,
    "escape": 53,
    "esc": 53,
    "delete": 51,
    "backspace": 51,
    "forward_delete": 117,
    "home": 115,
    "end": 119,
    "page_up": 116,
    "page_down": 121,
    "left": 123,
    "right": 124,
    "up": 126,
    "down": 125,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "space": 49,
}

_MOD_MAP = {
    "cmd": "command",
    "command": "command",
    "shift": "shift",
    "ctrl": "control",
    "control": "control",
    "alt": "option",
    "option": "option",
}


@register_node
class MouseMoveNode(BaseNode):
    name = "mouse_move"
    display_name = "鼠标移动"
    category = NodeCategory.MACOS_SYSTEM
    description = "移动鼠标到指定坐标"
    icon = "🖱️"
    default_label = "鼠标移动"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X 坐标 (像素)"},
                "y": {"type": "integer", "description": "Y 坐标 (像素)"},
                "duration": {"type": "number", "description": "移动持续时间 (秒), 0=瞬移", "default": 0},
            },
            "required": ["x", "y"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        x = self.config.params.get("x", inputs.get("x", 0))
        y = self.config.params.get("y", inputs.get("y", 0))
        duration = self.config.params.get("duration", 0)

        logger.info(f"mouse_move: ({x}, {y}), duration={duration}")

        if not _try_pyobjc_move(int(x), int(y)):
            rc, out = _applescript_move(int(x), int(y))

        if duration and duration > 0:
            await asyncio.sleep(duration)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"x": int(x), "y": int(y)},
            summary=f"鼠标已移动到 ({x}, {y})",
        )


@register_node
class MouseClickNode(BaseNode):
    name = "mouse_click"
    display_name = "鼠标点击"
    category = NodeCategory.MACOS_SYSTEM
    description = "鼠标点击 (左/右/双击)"
    icon = "🖱️"
    default_label = "鼠标点击"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X 坐标 (像素), 不指定则点击当前位置"},
                "y": {"type": "integer", "description": "Y 坐标 (像素)"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "鼠标按键",
                },
                "click_count": {"type": "integer", "default": 1, "description": "点击次数 (1=单击, 2=双击)"},
            },
            "required": [],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        x = self.config.params.get("x", inputs.get("x"))
        y = self.config.params.get("y", inputs.get("y"))
        button = self.config.params.get("button", "left")
        click_count = self.config.params.get("click_count", 1)

        logger.info(f"mouse_click: ({x}, {y}), button={button}, count={click_count}")

        if x is not None and y is not None:
            if not _try_pyobjc_click(int(x), int(y), button, int(click_count)):
                rc, out = _applescript_click(int(x), int(y), button, int(click_count))
                if rc != 0:
                    return NodeResult(status=NodeStatus.FAILED, error=f"鼠标点击失败: rc={rc}")
        else:
            rc, out = _run_osascript('tell application "System Events" to click at {0, 0}')
            if rc != 0:
                return NodeResult(status=NodeStatus.FAILED, error=f"鼠标点击失败: rc={rc}")

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"x": x, "y": y, "button": button, "click_count": click_count},
            summary=f"鼠标点击: button={button}, count={click_count}",
        )


@register_node
class KeyboardTypeNode(BaseNode):
    name = "keyboard_type"
    display_name = "键盘输入"
    category = NodeCategory.MACOS_SYSTEM
    description = "键盘输入文本"
    icon = "⌨️"
    default_label = "键盘输入"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本"},
                "delay": {"type": "number", "default": 0, "description": "每个字符间的延迟 (秒)"},
            },
            "required": ["text"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        text = self.config.params.get("text", inputs.get("text", ""))
        delay = self.config.params.get("delay", 0)

        if not text:
            return NodeResult(status=NodeStatus.FAILED, error="未指定输入文本")

        logger.info(f"keyboard_type: text='{text[:50]}...', delay={delay}")

        if not _try_pyobjc_type(text):
            rc, out = _applescript_type(text)
            if rc != 0:
                return NodeResult(status=NodeStatus.FAILED, error=f"键盘输入失败: rc={rc}")

        if delay and delay > 0:
            await asyncio.sleep(delay)

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"text": text, "char_count": len(text)},
            summary=f"键盘输入: {len(text)} 字符",
        )


@register_node
class KeyboardShortcutNode(BaseNode):
    name = "keyboard_shortcut"
    display_name = "键盘快捷键"
    category = NodeCategory.MACOS_SYSTEM
    description = "键盘快捷键 (Cmd+C, Cmd+V 等)"
    icon = "⌨️"
    default_label = "键盘快捷键"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "按键名称 (如 'c', 'v', 'enter', 'tab')"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["cmd", "shift", "ctrl", "alt"]},
                    "description": "修饰键 (cmd/shift/ctrl/alt)",
                    "default": [],
                },
                "key_code": {"type": "integer", "description": "macOS 键码 (可选, 优先于 key)"},
            },
            "required": ["key"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        key = self.config.params.get("key", inputs.get("key", ""))
        modifiers = self.config.params.get("modifiers", [])
        key_code = self.config.params.get("key_code")

        logger.info(f"keyboard_shortcut: key={key}, modifiers={modifiers}, key_code={key_code}")

        apple_mods = [_MOD_MAP.get(m, m) for m in modifiers]

        if key_code is not None:
            rc, out = _applescript_key_code(int(key_code), apple_mods)
        elif key.lower() in _KEY_MAP:
            rc, out = _applescript_key_code(_KEY_MAP[key.lower()], apple_mods)
        else:
            rc, out = _applescript_keystroke(key, apple_mods)

        if rc != 0:
            return NodeResult(status=NodeStatus.FAILED, error=f"快捷键执行失败: rc={rc}")

        mod_str = "+".join(modifiers + [key]) if modifiers else key
        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={"key": key, "modifiers": modifiers, "key_code": key_code},
            summary=f"快捷键: {mod_str}",
        )


@register_node
class ComputerUseLoopNode(BaseNode):
    name = "computer_use_loop"
    display_name = "Computer Use 循环"
    category = NodeCategory.MACOS_SYSTEM
    description = "截图→AI分析→操作→再截图的闭环控制"
    icon = "🤖"
    default_label = "Computer Use"

    def get_params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "目标任务描述 (如 '打开 Safari 并搜索天气')"},
                "max_steps": {"type": "integer", "default": 10, "description": "最大循环步数"},
                "step_delay": {"type": "number", "default": 1.0, "description": "每步间隔 (秒)"},
                "model": {"type": "string", "default": "default", "description": "fusion-mlx 模型名"},
                "screenshot_path": {"type": "string", "default": "", "description": "截图保存路径 (空则临时)"},
            },
            "required": ["task"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> NodeResult:
        task = self.config.params.get("task", inputs.get("task", ""))
        max_steps = self.config.params.get("max_steps", 10)
        step_delay = self.config.params.get("step_delay", 1.0)
        model = self.config.params.get("model", "default")
        # HI-10: 默认用临时文件, 用完即删; 显式 save_screenshots 才落盘到 screenshot_path
        save_screenshots = self.config.params.get("save_screenshots", False)

        if not task:
            return NodeResult(status=NodeStatus.FAILED, error="未指定任务描述")

        logger.info(f"computer_use_loop: task='{task}', max_steps={max_steps}")

        from ...ai import FusionMLXClient
        from .system_nodes import ScreenCaptureNode

        client = FusionMLXClient()
        steps_taken = 0
        actions_log = []

        for step in range(int(max_steps)):
            steps_taken += 1
            logger.info(f"computer_use step {step + 1}/{max_steps}")

            # HI-10: 用临时文件存截图, try/finally 删除 (修 param 名 output_dir→save_path + data 键 path→file_path)
            shot_path = ""
            tmp_handle = None
            if not save_screenshots:
                tmp_handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                shot_path = tmp_handle.name
                tmp_handle.close()
            else:
                shot_path = self.config.params.get("screenshot_path", "") or tempfile.gettempdir()

            capture = ScreenCaptureNode(
                config=NodeConfig(
                    params={
                        "save_path": shot_path,
                    }
                )
            )
            cap_result = await capture.execute({})

            if cap_result.status != NodeStatus.SUCCESS:
                logger.warning(f"截图失败: {cap_result.error}")
                actions_log.append({"step": step + 1, "action": "screenshot", "status": "failed"})
                if not save_screenshots and shot_path:
                    try:
                        os.unlink(shot_path)
                    except OSError:
                        pass
                continue

            # HI-10: ScreenCaptureNode 返回 data 键为 file_path, 非旧代码误读的 path
            screenshot_path = cap_result.data.get("file_path", "") or shot_path
            actions_log.append({"step": step + 1, "action": "screenshot", "path": screenshot_path})

            try:
                with open(screenshot_path, "rb") as f:
                    _img_b64 = base64.b64encode(f.read()).decode()
            except Exception as e:
                logger.warning(f"读取截图失败: {e}")
                continue

            prompt = (
                f"你是一个桌面自动化助手。当前任务: {task}\n\n"
                f"请分析当前屏幕截图，判断任务是否已完成。\n"
                f"如果已完成，回复: DONE\n"
                f"如果未完成，回复需要执行的操作，格式:\n"
                f"ACTION: <操作类型> PARAMS: <参数JSON>\n\n"
                f"操作类型:\n"
                f'- mouse_move: PARAMS: {{"x": 数字, "y": 数字}}\n'
                f'- mouse_click: PARAMS: {{"x": 数字, "y": 数字, "button": "left/right"}}\n'
                f'- keyboard_type: PARAMS: {{"text": "要输入的文本"}}\n'
                f'- keyboard_shortcut: PARAMS: {{"key": "按键", "modifiers": ["cmd"]}}\n'
            )

            try:
                content = _build_vision_user_content(prompt, screenshot_path)
                response = await client.chat(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                )
                ai_text = (
                    response.content
                    if hasattr(response, "content")
                    else (response.get("content", "") if isinstance(response, dict) else str(response))
                )
            except Exception as e:
                logger.warning(f"AI 分析失败: {e}")
                actions_log.append({"step": step + 1, "action": "ai_analyze", "status": "failed", "error": str(e)})
                continue

            actions_log.append({"step": step + 1, "action": "ai_analyze", "response": ai_text[:200]})

            if "DONE" in ai_text.upper():
                logger.info(f"computer_use: 任务完成于 step {step + 1}")
                actions_log.append({"step": step + 1, "action": "task_complete"})
                break

            action_result = await self._execute_ai_action(ai_text)
            actions_log.append({"step": step + 1, "action": "execute", "result": action_result})

            if step_delay and step_delay > 0:
                await asyncio.sleep(float(step_delay))
        # HI-10: 循环结束清理最后一张临时截图 (每步 continue/break 后临时文件仍可能残留)
        if not save_screenshots and screenshot_path:
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass

        return NodeResult(
            status=NodeStatus.SUCCESS,
            data={
                "task": task,
                "steps_taken": steps_taken,
                "actions": actions_log,
                "completed": any(a.get("action") == "task_complete" for a in actions_log),
            },
            summary=f"Computer Use: {steps_taken} 步, 任务={'完成' if any(a.get('action') == 'task_complete' for a in actions_log) else '未完成'}",
        )

    # CR-9: AI 解析动作白名单 + 每高危动作参数校验 (动作本身已在 HIGH_RISK_NODES 节点级 gate)
    _ACTION_WHITELIST = frozenset({"mouse_move", "mouse_click", "keyboard_type", "keyboard_shortcut"})
    _ACTION_NODES = {
        "mouse_move": "MouseMoveNode",
        "mouse_click": "MouseClickNode",
        "keyboard_type": "KeyboardTypeNode",
        "keyboard_shortcut": "KeyboardShortcutNode",
    }

    @classmethod
    def _validate_action_params(cls, action: str, params: Dict[str, Any]) -> Optional[str]:
        """CR-9: 校验每动作必填参数, 返回错误字符串 (None=合法)。"""
        if action in ("mouse_move", "mouse_click"):
            for k in ("x", "y"):
                v = params.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    return f"{action} 缺少/非法坐标 '{k}'"
        elif action == "keyboard_type":
            if not isinstance(params.get("text"), str) or not params["text"]:
                return "keyboard_type 缺少 text"
        elif action == "keyboard_shortcut":
            if not isinstance(params.get("key"), str) or not params["key"]:
                return "keyboard_shortcut 缺少 key"
        return None

    async def _execute_ai_action(self, ai_text: str) -> Dict[str, Any]:
        import json
        import re

        action_match = re.search(r"ACTION:\s*(\w+)", ai_text)
        params_match = re.search(r"PARAMS:\s*(\{[^}]+\})", ai_text)

        if not action_match:
            return {"status": "no_action", "text": ai_text[:100]}

        action = action_match.group(1).strip()
        # CR-9: 动作白名单 — 拒绝 AI 输出任何未登记动作 (防注入未授权操作)
        if action not in self._ACTION_WHITELIST:
            logger.warning(f"computer_use 拒绝非白名单动作: {action}")
            return {"status": "unknown_action", "action": action}

        params = {}
        if params_match:
            try:
                params = json.loads(params_match.group(1))
            except json.JSONDecodeError:
                params = {}

        # CR-9: 每动作参数校验
        err = self._validate_action_params(action, params)
        if err:
            logger.warning(f"computer_use 动作参数非法: {err}, action={action}")
            return {"status": "invalid_params", "action": action, "error": err}

        logger.info(f"computer_use action: {action}, params={params}")

        try:
            node_cls_name = self._ACTION_NODES[action]
            node_cls = globals().get(node_cls_name)
            if node_cls is None:
                return {"status": "error", "error": f"节点 {node_cls_name} 未找到"}
            node = node_cls(config=NodeConfig(params=params))
            result = await node.execute(params)

            return {"status": result.status.value, "data": result.data}
        except Exception as e:
            logger.error(f"computer_use action failed: {e}")
            return {"status": "error", "error": str(e)}
