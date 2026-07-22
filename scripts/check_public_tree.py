#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_NODE_LICENSES = {"MIT", "ISC", "Apache-2.0", "BSD-3-Clause", "CC-BY-4.0"}
FORBIDDEN_DEPENDENCY_TERMS = ("pymupdf", "pymupdf4llm", "agpl", "sspl")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, stdout=subprocess.PIPE,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> None:
    errors: list[str] = []
    files = _tracked_files()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        lowered = relative.casefold()
        if path.name == ".env" or lowered.endswith(FORBIDDEN_SUFFIXES):
            errors.append(f"禁止公开的敏感文件：{relative}")
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".docx"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name != Path(__file__).name and (
            "BEGIN PRIVATE KEY" in text or "BEGIN OPENSSH PRIVATE KEY" in text
        ):
            errors.append(f"疑似私钥内容：{relative}")

    requirements = (ROOT / "server" / "requirements.txt").read_text(encoding="utf-8").casefold()
    for term in FORBIDDEN_DEPENDENCY_TERMS:
        if term in requirements:
            errors.append(f"后端依赖包含禁止许可证组件：{term}")

    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    for package_path, metadata in lock.get("packages", {}).items():
        license_name = metadata.get("license")
        if package_path and license_name not in ALLOWED_NODE_LICENSES:
            errors.append(f"Node 依赖许可证未批准：{package_path} ({license_name or 'unknown'})")

    prohibited_terms = [
        item.strip().casefold()
        for item in os.getenv("PUBLIC_PROHIBITED_TERMS", "").split(",")
        if item.strip()
    ]
    if prohibited_terms:
        for path in files:
            if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".docx"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            for term in prohibited_terms:
                if term in text:
                    errors.append(f"发现禁止公开的企业标识：{path.relative_to(ROOT).as_posix()}")
                    break

    if errors:
        print("公开仓库检查失败：", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"公开仓库检查通过：{len(files)} 个已跟踪文件。")


if __name__ == "__main__":
    main()
