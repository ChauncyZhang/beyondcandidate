#!/usr/bin/env python3
import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "deploy" / ".env"
EXAMPLE_FILE = ROOT / "deploy" / ".env.example"


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _write_env(values: dict[str, str]) -> None:
    keys = list(_parse_env(EXAMPLE_FILE))
    keys.extend(key for key in values if key not in keys)
    content = "\n".join(f"{key}={values[key]}" for key in keys if key in values) + "\n"
    ENV_FILE.write_text(content, encoding="utf-8", newline="\n")


def _compose(*arguments: str, environment: dict[str, str] | None = None) -> None:
    command = [
        "docker", "compose", "--env-file", str(ENV_FILE),
        "-f", str(ROOT / "deploy" / "compose.yaml"), *arguments,
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)


def _wait_until_ready(port: int, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health/ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise RuntimeError(f"服务未在 {timeout_seconds} 秒内就绪，请运行 docker compose logs 查看原因。")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化并启动 BeyondCandidate 社区版")
    parser.add_argument("--organization-slug", default="beyondcandidate")
    parser.add_argument("--organization-name", default="BeyondCandidate")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--admin-name", default="系统管理员")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间。")
    if shutil.which("docker") is None:
        raise SystemExit("未找到 Docker。请先安装 Docker Desktop 或 Docker Engine + Compose plugin。")

    first_run = not ENV_FILE.exists()
    values = _parse_env(ENV_FILE if ENV_FILE.exists() else EXAMPLE_FILE)
    generated = {
        "POSTGRES_PASSWORD": _secret(),
        "APP_DB_PASSWORD": _secret(),
        "GOVERNANCE_DB_PASSWORD": _secret(),
        "MINIO_ROOT_USER": "bc-root-" + secrets.token_hex(6),
        "MINIO_ROOT_PASSWORD": _secret(),
        "APP_OBJECT_STORAGE_ACCESS_KEY": "bc-app-" + secrets.token_hex(6),
        "APP_OBJECT_STORAGE_SECRET_KEY": _secret(),
        "GOVERNANCE_DELETE_ACCESS_KEY": "bc-delete-" + secrets.token_hex(6),
        "GOVERNANCE_DELETE_SECRET_KEY": _secret(),
        "GOVERNANCE_LEDGER_ACCESS_KEY": "bc-ledger-" + secrets.token_hex(6),
        "GOVERNANCE_LEDGER_SECRET_KEY": _secret(),
        "GOVERNANCE_LEDGER_SIGNING_KEY": _secret(48),
        "CONTACT_ENCRYPTION_KEY": _fernet_key(),
        "CONTACT_LOOKUP_SECRET": _secret(48),
        "LLM_CONFIG_ENCRYPTION_KEY": _fernet_key(),
        "FEISHU_CONFIG_ENCRYPTION_KEY": _fernet_key(),
        "EMAIL_ENCRYPTION_KEY": _fernet_key(),
    }
    for key, value in generated.items():
        if first_run or not values.get(key) or values[key].startswith(("change-me", "replace-me", "placeholder")):
            values[key] = value
    values.update({
        "APP_ENVIRONMENT": "development",
        "DEFAULT_ORGANIZATION_SLUG": args.organization_slug,
        "DEFAULT_ORGANIZATION_NAME": args.organization_name,
        "HTTP_PORT": str(args.port),
        "CORS_ORIGINS": json.dumps([f"http://localhost:{args.port}"], ensure_ascii=False, separators=(",", ":")),
        "OFFER_PUBLIC_BASE_URL": f"http://localhost:{args.port}",
    })
    _write_env(values)

    admin_password = args.admin_password or (_secret(18) if first_run else "")
    subprocess.run(["docker", "compose", "version"], check=True, stdout=subprocess.DEVNULL)
    _compose("build", "api", "proxy")
    _compose("up", "-d", "postgres", "minio", "clamav")
    _compose("run", "--rm", "minio-provision")

    owner_url = (
        "postgresql+asyncpg://"
        + urllib.parse.quote(values["POSTGRES_USER"], safe="")
        + ":" + urllib.parse.quote(values["POSTGRES_PASSWORD"], safe="")
        + "@postgres:5432/" + urllib.parse.quote(values["POSTGRES_DB"], safe="")
    )
    _compose("run", "--rm", "--no-deps", "-e", f"DATABASE_URL={owner_url}", "api", "python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head")
    _compose("exec", "-T", "postgres", "sh", "/docker-entrypoint-initdb.d/10-provision-app-role.sh")

    if admin_password:
        runtime = os.environ.copy()
        runtime.update({
            "BOOTSTRAP_ORGANIZATION_SLUG": args.organization_slug,
            "BOOTSTRAP_ORGANIZATION_NAME": args.organization_name,
            "BOOTSTRAP_ADMIN_EMAIL": args.admin_email,
            "BOOTSTRAP_ADMIN_DISPLAY_NAME": args.admin_name,
            "BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        })
        _compose(
            "run", "--rm", "--no-deps",
            "-e", "BOOTSTRAP_ORGANIZATION_SLUG", "-e", "BOOTSTRAP_ORGANIZATION_NAME",
            "-e", "BOOTSTRAP_ADMIN_EMAIL", "-e", "BOOTSTRAP_ADMIN_DISPLAY_NAME",
            "-e", "BOOTSTRAP_ADMIN_PASSWORD", "api",
            "python", "-m", "server.app.identity.bootstrap",
            environment=runtime,
        )

    _compose("up", "-d", "api", "worker", "proxy")
    _wait_until_ready(args.port)
    print(f"BeyondCandidate 已启动：http://localhost:{args.port}")
    if admin_password:
        print(f"管理员账号：{args.admin_email}")
        print(f"管理员初始密码：{admin_password}")
        print("该密码仅显示本次，请登录后立即修改并妥善保存。")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Docker 命令执行失败（退出码 {error.returncode}）。") from error
    except KeyboardInterrupt:
        raise SystemExit("已取消。") from None
