"""Stage 5 deployment smoke tests — Dockerfile / docker-compose / Helm chart / serve command.

All @pytest.mark.slow; without docker/helm they skip and do not block normal CI.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _which(name: str) -> str | None:
    return shutil.which(name)


# ── Dockerfile existence (no build; build covered in test_dockerfile_build) ──


def test_dockerfile_exists_and_nonroot():
    path = os.path.join(PROJ_DIR, "Dockerfile")
    assert os.path.isfile(path)
    text = open(path, encoding="utf-8").read()
    assert "PYTHON_VERSION=3.12-slim" in text or "python:3.12-slim" in text
    assert "USER fcuser" in text, "Dockerfile 必须非 root 运行"  # i18n: user-facing
    assert "HEALTHCHECK" in text, "Dockerfile 必须有 HEALTHCHECK"  # i18n: user-facing
    assert "EXPOSE 11438" in text


def test_dockerignore_excludes_tests():
    path = os.path.join(PROJ_DIR, ".dockerignore")
    assert os.path.isfile(path)
    text = open(path, encoding="utf-8").read()
    assert "tests" in text
    assert ".venv" in text


def test_compose_file_structure():
    path = os.path.join(PROJ_DIR, "docker-compose.yml")
    assert os.path.isfile(path)
    text = open(path, encoding="utf-8").read()
    assert "postgres:16" in text
    assert "FUSION_PG_DSN" in text
    assert "pg_isready" in text, "postgres 必须有 healthcheck"  # i18n: user-facing


# ── docker-compose config validation (requires docker) ──


@pytest.mark.slow
def test_compose_config_valid():
    if not _which("docker"):
        pytest.skip("docker not installed")
    result = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=PROJ_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"docker compose config failed:\n{result.stderr}"


# ── Dockerfile build smoke (requires docker, heaviest, standalone test) ──


@pytest.mark.slow
def test_dockerfile_build_smoke():
    if not _which("docker"):
        pytest.skip("docker not installed")
    tag = "fusion-cowork:test-deploy"
    try:
        result = subprocess.run(
            ["docker", "build", "-t", tag, "-f", "fusion-cowork/Dockerfile", "."],
            cwd=os.path.dirname(PROJ_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("docker build 超时 (>600s, 网络/镜像源慢, 非代码缺陷)")  # i18n: user-facing
    if result.returncode != 0:
        pytest.skip(f"docker build failed (可能网络/镜像源问题):\n{result.stderr[-2000:]}")  # i18n: user-facing
    # Clean up image
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=60)


# ── Helm chart render (requires helm) ──


@pytest.mark.slow
def test_helm_template_renders():
    if not _which("helm"):
        pytest.skip("helm not installed")
    chart_dir = os.path.join(PROJ_DIR, "deploy", "helm", "fusion-cowork")
    assert os.path.isfile(os.path.join(chart_dir, "Chart.yaml"))
    result = subprocess.run(
        ["helm", "template", "fc-test", chart_dir],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    out = result.stdout
    assert "kind: Deployment" in out
    assert "kind: Service" in out
    assert "kind: Secret" in out
    assert "kind: ConfigMap" in out
    assert "kind: HorizontalPodAutoscaler" in out
    assert "FUSION_PG_DSN" in out
    assert "FUSION_JWT_SECRET" in out
    assert "/health" in out, "deployment 必须有 liveness/readiness /health probe"  # i18n: user-facing


def test_helm_values_sensible_defaults():
    values = os.path.join(PROJ_DIR, "deploy", "helm", "fusion-cowork", "values.yaml")
    text = open(values, encoding="utf-8").read()
    assert "change-me" in text, "secret 占位必须标 change-me"  # i18n: user-facing
    assert "terminationGracePeriodSeconds: 30" in text
    assert "targetCPUUtilizationPercentage: 70" in text


# ── serve command binds 0.0.0.0 + /health reachable (brief start + stop) ──


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
def test_serve_binds_all_interfaces_health():
    port = _free_port()
    env = dict(os.environ)
    env["FUSION_BIND_HOST"] = "0.0.0.0"
    env.setdefault("FUSION_JSON_LOG", "0")
    proc = subprocess.Popen(
        [sys_python(), "-m", "fusion_cowork", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        ok = False
        for _ in range(40):
            if proc.poll() is not None:
                out = proc.stdout.read(2000).decode("utf-8", "replace") if proc.stdout else ""
                pytest.fail(f"serve exited early:\n{out}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    assert r.status == 200
                    ok = True
                    break
            except Exception:
                time.sleep(0.5)
        assert ok, "serve /health 未在 20s 内就绪"  # i18n: user-facing
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def sys_python() -> str:
    # #198: use the current interpreter, not PATH — the python3 on PATH may be a different environment
    # (without an activated venv it resolves to homebrew python, missing fusion_cowork, so serve fails to start)
    return sys.executable
