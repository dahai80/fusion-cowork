"""macOS 鼠标键盘控制节点 — Computer Use 核心。

通过 AppleScript/osascript 实现鼠标移动、点击、键盘输入，
可选 pyobjc 提供更精确的像素级控制。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, List, Tuple

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
    script = f'''
    tell application "System Events"
        {click_cmd} {{{x}, {y}}}
    end tell
    '''
    return _run_osascript(script)


def _applescript_move(x: int, y: int) -> Tuple[int, str]:
    return 0, ""


def _applescript_type(text: str) -> Tuple[int, str]:
    escaped = text.replace('"', '\\"').replace('\\', '\\\\')
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
    script = f'''
    tell application "System Events"
        key code {key_code}{mod_str}
    end tell
    '''
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


_KEY_MAP = {
    "enter": 36, "return": 36,
    "tab": 48, "escape": 53, "esc": 53,
    "delete": 51, "backspace": 51,
    "forward_delete": 117,
    "home": 115, "end": 119,
    "page_up": 116, "page_down": 121,
    "left": 123, "right": 124, "up": 126, "down": 125,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
    "space": 49,
}

_MOD_MAP = {
    "cmd": "command", "command": "command",
    "shift": "shift", "ctrl": "control", "control": "control",
    "alt": "option", "option": "option",
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
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left", "description": "鼠标按键"},
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

            capture = ScreenCaptureNode(config=NodeConfig(params={
                "output_dir": self.config.params.get("screenshot_path", ""),
            }))
            cap_result = await capture.execute({})

            if cap_result.status != NodeStatus.SUCCESS:
                logger.warning(f"截图失败: {cap_result.error}")
                actions_log.append({"step": step + 1, "action": "screenshot", "status": "failed"})
                continue

            screenshot_path = cap_result.data.get("path", "")
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
                f"- mouse_move: PARAMS: {{\"x\": 数字, \"y\": 数字}}\n"
                f"- mouse_click: PARAMS: {{\"x\": 数字, \"y\": 数字, \"button\": \"left/right\"}}\n"
                f"- keyboard_type: PARAMS: {{\"text\": \"要输入的文本\"}}\n"
                f"- keyboard_shortcut: PARAMS: {{\"key\": \"按键\", \"modifiers\": [\"cmd\"]}}\n"
            )

            try:
                response = await client.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
                ai_text = response.get("content", "") if isinstance(response, dict) else str(response)
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

    async def _execute_ai_action(self, ai_text: str) -> Dict[str, Any]:
        import json
        import re

        action_match = re.search(r"ACTION:\s*(\w+)", ai_text)
        params_match = re.search(r"PARAMS:\s*(\{[^}]+\})", ai_text)

        if not action_match:
            return {"status": "no_action", "text": ai_text[:100]}

        action = action_match.group(1).strip()
        params = {}
        if params_match:
            try:
                params = json.loads(params_match.group(1))
            except json.JSONDecodeError:
                params = {}

        logger.info(f"computer_use action: {action}, params={params}")

        try:
            if action == "mouse_move":
                node = MouseMoveNode(config=NodeConfig(params=params))
                result = await node.execute(params)
            elif action == "mouse_click":
                node = MouseClickNode(config=NodeConfig(params=params))
                result = await node.execute(params)
            elif action == "keyboard_type":
                node = KeyboardTypeNode(config=NodeConfig(params=params))
                result = await node.execute(params)
            elif action == "keyboard_shortcut":
                node = KeyboardShortcutNode(config=NodeConfig(params=params))
                result = await node.execute(params)
            else:
                return {"status": "unknown_action", "action": action}

            return {"status": result.status.value, "data": result.data}
        except Exception as e:
            logger.error(f"computer_use action failed: {e}")
            return {"status": "error", "error": str(e)}
