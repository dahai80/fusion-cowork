import inspect
import subprocess
import sys
import time

import pytest

from fusion_cowork.cli import _set_process_title


class TestSetTitleHelper:
    def test_sets_title_when_setproctitle_available(self):
        setproctitle = pytest.importorskip("setproctitle")
        old = setproctitle.getproctitle()
        try:
            _set_process_title("fusion-cowork unit-test")
            assert setproctitle.getproctitle() == "fusion-cowork unit-test"
        finally:
            setproctitle.setproctitle(old)

    def test_fallback_direct_setproctitle_without_fusion_core(self, monkeypatch):
        # fusion_core.proc 不可用 → 直接走 setproctitle, 行为一致
        monkeypatch.setitem(sys.modules, "fusion_core", None)
        monkeypatch.setitem(sys.modules, "fusion_core.proc", None)
        setproctitle = pytest.importorskip("setproctitle")
        old = setproctitle.getproctitle()
        try:
            _set_process_title("fusion-cowork fallback-test")
            assert setproctitle.getproctitle() == "fusion-cowork fallback-test"
        finally:
            setproctitle.setproctitle(old)

    def test_no_raise_when_setproctitle_unavailable(self, monkeypatch):
        # 两条路径都不可用 → 静默降级 (标题保持默认), 绝不阻断入口
        monkeypatch.setitem(sys.modules, "fusion_core", None)
        monkeypatch.setitem(sys.modules, "fusion_core.proc", None)
        monkeypatch.setitem(sys.modules, "setproctitle", None)
        _set_process_title("fusion-cowork no-op")


class TestSubprocessTitle:
    def test_title_visible_in_ps(self):
        # 验收 #1 的机制证明: 子进程设标题后 ps -o comm 显示 fusion-cowork 前缀
        # (macOS comm 截断到 16 字符, 断言用前缀匹配)
        pytest.importorskip("setproctitle")
        code = (
            "from fusion_cowork.cli import _set_process_title; "
            "_set_process_title('fusion-cowork ps-check'); "
            "import time; time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = time.time() + 20
            seen = ""
            while time.time() < deadline:
                out = subprocess.run(
                    ["ps", "-o", "comm=", "-p", str(proc.pid)],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if out:
                    seen = out
                    if out.startswith("fusion-cowork"):
                        return
                time.sleep(0.3)
            pytest.fail(f"ps comm never showed fusion-cowork title, saw: {seen!r} (pid={proc.pid})")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


class TestEntrypointsWired:
    def test_all_long_running_entrypoints_set_title(self):
        from fusion_cowork import cli

        expected = {
            "desk_rpc": "fusion-cowork desk-rpc",
            "remote_serve": "fusion-cowork remote",
            "web_serve": "fusion-cowork web :",
            "serve": "fusion-cowork serve :",
        }
        for name, fragment in expected.items():
            fn = getattr(cli, name).callback
            src = inspect.getsource(fn)
            assert "_set_process_title(" in src, f"cli.{name} does not set process title"
            assert fragment in src, f"cli.{name} title missing expected fragment {fragment!r}"
