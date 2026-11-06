"""issue #347: claude_dispatch env 白名单透传 claude 认证变量 + 崩溃输出可诊断。

env 白名单此前只有 PATH/HOME/LANG/GITHUB_TOKEN — 依赖 ANTHROPIC_AUTH_TOKEN
认证的宿主上 claude CLI 未登录退出 1 (daemon 上下文无 Keychain 必挂)。
现在四个认证变量按存在透传; 非零退出时会话输出头并入错误。
"""

import stat

from tests.test_issue_308_claude_dispatch import _OK_RESULT, _fake_claude, _node

_PR_OK = f"#!/bin/bash\necho '{_OK_RESULT}'\n"


def _probe(tmp_path, probe_lines):
    probe = tmp_path / "probe-claude.sh"
    probe.write_text("#!/bin/bash\n" + "\n".join(probe_lines) + f"\necho '{_OK_RESULT}'\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return str(probe)


class TestClaudeDispatchEnv:
    async def test_auth_token_passthrough(self, tmp_path, monkeypatch):
        # 父环境有 ANTHROPIC_AUTH_TOKEN → 子进程可见; 缺失则模拟未登录退出 1
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-347")
        binp = _probe(
            tmp_path,
            ['if [ -z "$ANTHROPIC_AUTH_TOKEN" ]; then echo "Not logged in" >&2; exit 1; fi'],
        )
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "success", res.error

    async def test_all_auth_vars_passthrough(self, tmp_path, monkeypatch):
        for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            monkeypatch.setenv(var, f"v-{var}")
        dump = tmp_path / "env-dump.txt"
        binp = _probe(
            tmp_path,
            [
                'for v in ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL; do echo "$v=$(printenv $v)" >> '
                + str(dump)
                + "; done",
            ],
        )
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "success", res.error
        content = dump.read_text()
        assert "ANTHROPIC_API_KEY=v-ANTHROPIC_API_KEY" in content
        assert "CLAUDE_CODE_OAUTH_TOKEN=v-CLAUDE_CODE_OAUTH_TOKEN" in content
        assert "ANTHROPIC_BASE_URL=v-ANTHROPIC_BASE_URL" in content

    async def test_unauthenticated_crash_includes_output_head(self, tmp_path, monkeypatch):
        # 非零退出: 会话输出头并入错误 — 无需复现即可诊断 (issue 次要诉求)
        for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        binp = _fake_claude(tmp_path, ['echo "Not logged in · Please run /login" >&2'], exit_code=1)
        res = await _node(tmp_path, binp).execute({})
        assert res.status.value == "failed"
        assert "Not logged in" in res.error
        assert res.data["output_head"].startswith("Not logged in")
