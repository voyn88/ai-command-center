"""Тесты Phase 7: Docker-конфигурация + entrypoint."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _docker_daemon_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_daemon_reachable(),
    reason="docker недоступен — тест пропускается локально без Docker "
    "(тот же паттерн, что у tests/db для AICC_TEST_PG_ADMIN_DSN); CI собирает образ по-настоящему",
)


def test_dockerfile_exists() -> None:
    assert (ROOT / "Dockerfile").exists(), "Dockerfile отсутствует"


def test_dockerfile_syntax() -> None:
    """Dockerfile не должен содержать синтаксических ошибок (hadolint если доступен)."""
    dockerfile = ROOT / "Dockerfile"
    content = dockerfile.read_text()
    # Базовые структурные проверки
    assert "FROM python:3.13" in content, "Базовый образ должен быть python:3.13"
    assert "EXPOSE 8501" in content, "Порт 8501 должен быть задекларирован"
    assert "HEALTHCHECK" in content, "Healthcheck обязателен"
    assert "ENTRYPOINT" in content, "ENTRYPOINT должен быть задан"
    assert "AICC_DATA_DIR" in content, "Переменная AICC_DATA_DIR должна быть в ENV"


def test_docker_compose_exists() -> None:
    assert (ROOT / "docker-compose.aml.yml").exists(), "docker-compose.aml.yml отсутствует"


def test_docker_compose_content() -> None:
    compose = (ROOT / "docker-compose.aml.yml").read_text()
    assert "8501" in compose, "Порт 8501 должен быть в compose"
    assert "aml-data" in compose, "Том aml-data должен быть в compose"
    assert "AICC_DATA_DIR" in compose, "Переменная AICC_DATA_DIR должна быть в compose"
    assert "healthcheck" in compose, "Healthcheck обязателен в compose"


def test_dockerignore_exists() -> None:
    assert (ROOT / ".dockerignore").exists(), ".dockerignore отсутствует"


def test_dockerignore_excludes_data() -> None:
    ignore = (ROOT / ".dockerignore").read_text()
    assert "data/" in ignore, "Директория data/ должна быть исключена"
    assert ".venv/" in ignore or "venv/" in ignore, ".venv должен быть исключён"
    assert ".git/" in ignore, ".git должен быть исключён"


@requires_docker
def test_image_builds_from_clean_checkout(tmp_path: Path) -> None:
    """Собирает образ по-настоящему, а не грепает текст Dockerfile.

    Регрессия для найденного приёмкой PR #314 дефекта: `.dockerignore` исключал
    `scripts/`, из-за чего `COPY scripts/aml-entrypoint.sh` не резолвился и
    `docker build` падал из чистого чекаута (сломано с 63581e1 / #131).
    """
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "worktree", "add", "--detach", "-q", str(checkout), "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tag = f"aicc-aml-test-{uuid.uuid4().hex[:12]}"
    try:
        build = subprocess.run(
            ["docker", "build", "-t", tag, str(checkout)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert build.returncode == 0, (
            f"docker build упал из чистого чекаута:\n{build.stdout}\n{build.stderr}"
        )

        check = subprocess.run(
            [
                "docker", "run", "--rm",
                "--entrypoint", "/bin/sh",
                tag, "-c", "test -x /entrypoint.sh",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert check.returncode == 0, (
            f"/entrypoint.sh отсутствует или не исполняем в собранном образе:\n{check.stderr}"
        )
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, check=False)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(checkout)],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )


def test_entrypoint_exists() -> None:
    assert (ROOT / "scripts" / "aml-entrypoint.sh").exists(), "aml-entrypoint.sh отсутствует"


def test_entrypoint_content() -> None:
    entrypoint = (ROOT / "scripts" / "aml-entrypoint.sh").read_text()
    assert "seed_rules_115fz" in entrypoint, "Entrypoint должен вызывать seed_rules_115fz"
    assert "streamlit run" in entrypoint, "Entrypoint должен запускать streamlit"
    assert "set -euo pipefail" in entrypoint, "Entrypoint должен использовать строгий bash-режим"
    assert "AICC_DATA_DIR" in entrypoint, "Entrypoint должен использовать AICC_DATA_DIR"


def test_docs_aml_dir_exists() -> None:
    assert (ROOT / "docs" / "aml").is_dir(), "Директория docs/aml/ отсутствует"


def test_acceptance_package_exists() -> None:
    p = ROOT / "docs" / "aml" / "ACCEPTANCE_PACKAGE.md"
    assert p.exists(), "ACCEPTANCE_PACKAGE.md отсутствует"
    content = p.read_text()
    assert "115-ФЗ" in content
    assert "SAR" in content
    assert "docker compose" in content.lower() or "docker-compose" in content.lower()


def test_compliance_checklist_exists() -> None:
    p = ROOT / "docs" / "aml" / "COMPLIANCE_CHECKLIST.md"
    assert p.exists(), "COMPLIANCE_CHECKLIST.md отсутствует"
    content = p.read_text()
    # Все разделы должны присутствовать
    assert "## A." in content, "Раздел A (обязательный контроль) отсутствует"
    assert "## B." in content, "Раздел B (подозрительные операции) отсутствует"
    assert "## C." in content, "Раздел C (KYC) отсутствует"
    assert "100%" in content, "Процент выполнения должен быть указан"


def test_architecture_doc_exists() -> None:
    p = ROOT / "docs" / "aml" / "ARCHITECTURE.md"
    assert p.exists(), "ARCHITECTURE.md отсутствует"
    content = p.read_text()
    assert "alert_store" in content
    assert "case_store" in content
    assert "sar_store" in content
    assert "compliance_store" in content


def test_seed_module_importable() -> None:
    """Модуль seed_rules_115fz должен импортироваться без ошибок."""
    from command_center.seed_rules_115fz import seed
    assert callable(seed)


def test_seed_idempotent_in_fresh_db(tmp_path: Path) -> None:
    """Двойной вызов seed не должен приводить к дублям."""
    from command_center.seed_rules_115fz import seed

    db = tmp_path / "rules.db"
    n1 = seed(db)
    n2 = seed(db)
    assert n1 > 0, "Первый вызов должен создать правила"
    assert n2 == 0, "Второй вызов должен пропустить все (идемпотентность)"
