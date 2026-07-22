#!/usr/bin/env python3
"""S3-compatible COMPLETE-last publisher using a provider-native lease."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO, Mapping, Sequence
from urllib.parse import unquote, urlparse


EXIT_USAGE = 64
EXIT_PROVIDER_FAILURE = 74
EXIT_CONFLICT = 75
EXIT_COMMIT_UNKNOWN = 76
EXIT_SAFETY = 78
EXIT_INTERNAL = 70
MC_BIN = "/usr/local/bin/mc"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{3,126}[A-Za-z0-9])$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LEASE_PREFIX = ".ux09-private-leases"
BUSINESS_MEMBERS = frozenset({
    "database.dump",
    "business.snapshot",
    "inventory.jsonl",
    "reference-proof.json",
    "manifest.json",
    "manifest.sig",
    "COMPLETE",
})
LEDGER_MEMBERS = frozenset({
    "ledger.archive",
    "ledger-manifest.json",
    "ledger-manifest.sig",
    "COMPLETE",
})


class SafetyError(ValueError):
    pass


class ProviderFailure(RuntimeError):
    pass


class LeaseConflict(RuntimeError):
    pass


class ObjectMissing(RuntimeError):
    pass


class CommitUnknown(RuntimeError):
    pass


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _open_verified_regular(path: Path, *, protected: bool) -> BinaryIO:
    if _is_reparse(path):
        raise SafetyError("unsafe file")
    try:
        before = path.lstat()
    except OSError as error:
        raise SafetyError("missing file") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SafetyError("unsafe file")
    if protected and os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
        raise SafetyError("unprotected file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SafetyError("unsafe file") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (protected and os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077)
        ):
            raise SafetyError("file changed during validation")
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def _copy_open_file(source: BinaryIO, destination: Path) -> None:
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_directory_without_symlinks(path: Path) -> int:
    if os.name == "nt":
        if _is_reparse(path) or path.resolve(strict=False) != Path(os.path.abspath(path)):
            raise SafetyError("unsafe directory")
        try:
            return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as error:
            raise SafetyError("unsafe directory") from error
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SafetyError("unsafe directory")
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise SafetyError("unsafe directory") from error
    except Exception:
        os.close(descriptor)
        raise


def _open_source_directory(path: Path) -> int:
    return _open_directory_without_symlinks(path)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _copy_descriptor_to_snapshot(source_descriptor: int, destination: Path) -> tuple[int, str]:
    destination_descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)
    return size, digest.hexdigest()


def _copy_source_member(source_fd: int, name: str, destination: Path) -> tuple[int, str]:
    if name not in BUSINESS_MEMBERS | LEDGER_MEMBERS or "/" in name or "\\" in name:
        raise SafetyError("unsafe source member")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=source_fd)
    except OSError as error:
        raise SafetyError("unsafe source member") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SafetyError("unsafe source member")
        result = _copy_descriptor_to_snapshot(descriptor, destination)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise SafetyError("source member changed during snapshot")
        return result
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _validate_file_binding(evidence: object, size: int, digest: str, hash_field: str = "sha256") -> None:
    if not isinstance(evidence, Mapping):
        raise SafetyError("missing payload evidence")
    expected_hash = evidence.get(hash_field)
    expected_size = evidence.get("size_bytes")
    if (
        not isinstance(expected_hash, str)
        or not SHA256_RE.fullmatch(expected_hash)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise SafetyError("invalid payload evidence")
    if size != expected_size or digest != expected_hash:
        raise SafetyError("payload does not match manifest")


def _validate_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise SafetyError("invalid run id")
    return value


def _parse_destination(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or "\\" in value or "%" in value:
        raise SafetyError("invalid destination")
    parsed = urlparse(value)
    if parsed.scheme not in {"s3", "minio"} or parsed.username or parsed.password:
        raise SafetyError("invalid destination")
    if parsed.query or parsed.fragment or parsed.params:
        raise SafetyError("invalid destination")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if any(part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        raise SafetyError("invalid destination")
    if parsed.scheme == "s3":
        alias = "s3"
        bucket = parsed.netloc
        prefix_parts = parts
    else:
        alias = parsed.netloc
        if not parts:
            raise SafetyError("invalid destination")
        bucket, *prefix_parts = parts
    if not ALIAS_RE.fullmatch(alias) or not BUCKET_RE.fullmatch(bucket):
        raise SafetyError("invalid destination")
    prefix = "/".join(prefix_parts)
    root = f"{alias}/{bucket}" + (f"/{prefix}" if prefix else "")
    lease_root = f"{alias}/{bucket}/{LEASE_PREFIX}"
    return alias, root, lease_root


def _validate_config(config: Path, alias: str, destination: Path) -> None:
    with _open_verified_regular(config, protected=True) as source:
        payload = source.read()
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SafetyError("invalid client config") from error
        aliases = document.get("aliases") if isinstance(document, Mapping) else None
        if not isinstance(aliases, Mapping) or alias not in aliases:
            raise SafetyError("invalid client config")
        _copy_open_file(io.BytesIO(payload), destination)


def _validate_snapshot(
    destination: Path,
    run_id: str,
    names: frozenset[str],
    metadata: Mapping[str, tuple[int, str]],
) -> tuple[list[str], str]:
    if names not in {BUSINESS_MEMBERS, LEDGER_MEMBERS}:
        raise SafetyError("unexpected source members")
    manifest_name = "manifest.json" if names == BUSINESS_MEMBERS else "ledger-manifest.json"
    manifest_payload = (destination / manifest_name).read_bytes()
    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SafetyError("invalid manifest") from error
    run_field = "backup_run_id" if names == BUSINESS_MEMBERS else "archive_run_id"
    if not isinstance(manifest, Mapping) or manifest.get(run_field) != run_id:
        raise SafetyError("manifest run binding is invalid")
    if names == BUSINESS_MEMBERS:
        _validate_file_binding(manifest.get("database"), *metadata["database.dump"])
        business = manifest.get("business_snapshot")
        _validate_file_binding(business, *metadata["business.snapshot"])
        if not isinstance(business, Mapping):
            raise SafetyError("missing payload evidence")
        inventory_size, inventory_hash = metadata["inventory.jsonl"]
        _validate_file_binding(
            {"sha256": business.get("inventory_sha256"), "size_bytes": inventory_size},
            inventory_size,
            inventory_hash,
        )
    else:
        _validate_file_binding(
            {"sha256": manifest.get("archive_sha256"), "size_bytes": manifest.get("size_bytes")},
            *metadata["ledger.archive"],
        )
    complete = (destination / "COMPLETE").read_bytes()
    expected = hashlib.sha256(manifest_payload).hexdigest()
    try:
        marker = complete.decode("ascii")
    except UnicodeDecodeError as error:
        raise SafetyError("invalid COMPLETE") from error
    if marker != expected + "\n" or not SHA256_RE.fullmatch(expected):
        raise SafetyError("invalid COMPLETE")
    return sorted(names - {"COMPLETE"}), expected


def _snapshot_source_windows(source: Path, run_id: str, destination: Path) -> tuple[list[str], str]:
    if _is_reparse(source):
        raise SafetyError("unsafe source")
    try:
        names = frozenset(entry.name for entry in os.scandir(source))
    except OSError as error:
        raise SafetyError("unsafe source") from error
    if names not in {BUSINESS_MEMBERS, LEDGER_MEMBERS}:
        raise SafetyError("unexpected source members")
    metadata: dict[str, tuple[int, str]] = {}
    for name in sorted(names):
        with _open_verified_regular(source / name, protected=False) as handle:
            destination_path = destination / name
            _copy_open_file(handle, destination_path)
        metadata[name] = (destination_path.stat().st_size, hashlib.sha256(destination_path.read_bytes()).hexdigest())
    return _validate_snapshot(destination, run_id, names, metadata)


def _snapshot_source(source: Path, run_id: str, destination: Path) -> tuple[list[str], str]:
    if os.name == "nt":
        return _snapshot_source_windows(source, run_id, destination)
    source_fd = _open_source_directory(source)
    try:
        names = frozenset(os.listdir(source_fd))
        if names not in {BUSINESS_MEMBERS, LEDGER_MEMBERS}:
            raise SafetyError("unexpected source members")
        metadata = {
            name: _copy_source_member(source_fd, name, destination / name)
            for name in sorted(names)
        }
    finally:
        os.close(source_fd)
    return _validate_snapshot(destination, run_id, names, metadata)


def _safe_environment(home: Path) -> dict[str, str]:
    environment: dict[str, str] = {"HOME": str(home), "MC_QUIET": "1", "MC_DISABLE_PAGER": "1"}
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _provider_error_codes(payload: bytes) -> set[str]:
    codes: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).lower() == "code" and isinstance(child, str):
                    codes.add(child.lower())
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in payload.splitlines():
        try:
            visit(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return codes


def _execute_mc(command: Sequence[str], *, stdin: BinaryIO | None = None, capture_stdout: bool = False) -> bytes:
    try:
        result = subprocess.run(
            list(command),
            input=stdin.read() if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_safe_environment(Path(command[command.index("-C") + 1])),
            check=False,
        )
    except OSError as error:
        raise ProviderFailure() from error
    if result.returncode == 0:
        return result.stdout if capture_stdout else b""
    output = result.stdout + b"\n" + result.stderr
    lowered = output.lower()
    codes = _provider_error_codes(output)
    if "pipe" in command and (
        codes.intersection({"preconditionfailed", "conditionalrequestconflict"})
        or b"precondition failed" in lowered
        or b"already exists" in lowered
    ):
        raise LeaseConflict()
    if "stat" in command and (
        codes.intersection({"nosuchkey", "nosuchobject", "pathnotfound"})
        or b"not found" in lowered
        or b"does not exist" in lowered
    ):
        raise ObjectMissing()
    raise ProviderFailure()


def _mc_command(config_dir: Path, operation: str, *arguments: str) -> list[str]:
    return [MC_BIN, "-C", str(config_dir), "--quiet", "--json", operation, *arguments]


def _object_exists(config_dir: Path, target: str) -> bool:
    try:
        _execute_mc(_mc_command(config_dir, "stat", "--no-list", target), capture_stdout=True)
        return True
    except ObjectMissing:
        return False


def _verify_remote(config_dir: Path, target: str, local: Path, verification: Path) -> None:
    raw = _execute_mc(_mc_command(config_dir, "stat", "--no-list", target), capture_stdout=True)
    try:
        stat_value = json.loads(raw.splitlines()[-1])
        remote_size = int(stat_value["size"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderFailure() from error
    if remote_size != local.stat().st_size:
        raise ProviderFailure()
    verification.unlink(missing_ok=True)
    _execute_mc(_mc_command(config_dir, "get", target, str(verification)))
    if verification.stat().st_size != local.stat().st_size:
        raise ProviderFailure()
    if _sha256_file(verification) != _sha256_file(local):
        raise ProviderFailure()


def _download_remote(config_dir: Path, target: str, destination: Path) -> None:
    raw = _execute_mc(_mc_command(config_dir, "stat", "--no-list", target), capture_stdout=True)
    try:
        stat_value = json.loads(raw.splitlines()[-1])
        remote_size = int(stat_value["size"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderFailure() from error
    _execute_mc(_mc_command(config_dir, "get", target, str(destination)))
    if not destination.is_file() or destination.stat().st_size != remote_size:
        raise ProviderFailure()


def _open_receipt_parent(path: Path) -> int:
    return _open_directory_without_symlinks(path)


def _write_receipt(path: Path, value: Mapping[str, object]) -> None:
    if path.name in {"", ".", ".."} or path.parent == path:
        raise SafetyError("unsafe receipt")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if os.name == "nt":
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        except OSError as error:
            raise SafetyError("unsafe receipt") from error
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    parent_fd = _open_receipt_parent(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise SafetyError("unsafe receipt") from error
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _parse_args(argv: Sequence[str]) -> dict[str, str]:
    if not argv or argv[0] not in {"publish-complete-group", "reconcile-complete-group"}:
        raise SafetyError("invalid command")
    operation = argv[0]
    expected = {"--lease-config-file", "--destination", "--run-id", "--receipt"}
    if operation == "publish-complete-group":
        expected.add("--source")
    if len(argv) != 1 + len(expected) * 2:
        raise SafetyError("invalid command")
    result: dict[str, str] = {}
    index = 1
    while index < len(argv):
        name = argv[index]
        if name not in expected or name in result or index + 1 >= len(argv):
            raise SafetyError("invalid command")
        value = argv[index + 1]
        if not value or "\x00" in value:
            raise SafetyError("invalid command")
        result[name] = value
        index += 2
    if set(result) != expected:
        raise SafetyError("invalid command")
    result["operation"] = operation
    return result


def _publish(arguments: Mapping[str, str]) -> None:
    run_id = _validate_run_id(arguments["--run-id"])
    alias, group_root, lease_root = _parse_destination(arguments["--destination"])
    config = Path(arguments["--lease-config-file"])
    source = Path(arguments["--source"])
    receipt = Path(arguments["--receipt"])
    with tempfile.TemporaryDirectory(prefix="ux09-publisher-") as private_name:
        private = Path(private_name)
        os.chmod(private, 0o700)
        config_dir = private / "mc"
        source_dir = private / "source"
        verification_dir = private / "verify"
        config_dir.mkdir(mode=0o700)
        source_dir.mkdir(mode=0o700)
        verification_dir.mkdir(mode=0o700)
        _validate_config(config, alias, config_dir / "config.json")
        payload_names, complete_hash = _snapshot_source(source, run_id, source_dir)
        lease_value = secrets.token_bytes(32)
        lease_target = f"{lease_root}/{run_id}"
        _execute_mc(
            [
                MC_BIN, "-C", str(config_dir), "--quiet", "--json",
                "--custom-header", "If-None-Match:*", "pipe", lease_target,
            ],
            stdin=io.BytesIO(lease_value),
        )
        complete_target = f"{group_root}/{run_id}/COMPLETE"
        if _object_exists(config_dir, complete_target):
            raise LeaseConflict()
        for name in payload_names:
            local = source_dir / name
            target = f"{group_root}/{run_id}/{name}"
            _execute_mc(_mc_command(config_dir, "put", "--checksum", "SHA256", str(local), target))
            _verify_remote(config_dir, target, local, verification_dir / name)
        complete = source_dir / "COMPLETE"
        try:
            _execute_mc(_mc_command(config_dir, "put", "--checksum", "SHA256", str(complete), complete_target))
            _verify_remote(config_dir, complete_target, complete, verification_dir / "COMPLETE")
            _write_receipt(receipt, {
                "schema_version": 1,
                "status": "committed",
                "backup_run_id": run_id,
                "complete_sha256": complete_hash,
                "lease_id_hash": hashlib.sha256(lease_value).hexdigest(),
            })
        except Exception as error:
            raise CommitUnknown() from error


def _reconcile(arguments: Mapping[str, str]) -> None:
    run_id = _validate_run_id(arguments["--run-id"])
    alias, group_root, lease_root = _parse_destination(arguments["--destination"])
    config = Path(arguments["--lease-config-file"])
    receipt = Path(arguments["--receipt"])
    with tempfile.TemporaryDirectory(prefix="ux09-reconcile-") as private_name:
        private = Path(private_name)
        os.chmod(private, 0o700)
        config_dir = private / "mc"
        remote_dir = private / "remote"
        config_dir.mkdir(mode=0o700)
        remote_dir.mkdir(mode=0o700)
        _validate_config(config, alias, config_dir / "config.json")
        try:
            lease = remote_dir / "lease"
            _download_remote(config_dir, f"{lease_root}/{run_id}", lease)
            business_manifest = f"{group_root}/{run_id}/manifest.json"
            ledger_manifest = f"{group_root}/{run_id}/ledger-manifest.json"
            business_exists = _object_exists(config_dir, business_manifest)
            ledger_exists = _object_exists(config_dir, ledger_manifest)
            if business_exists == ledger_exists:
                raise ProviderFailure()
            names = BUSINESS_MEMBERS if business_exists else LEDGER_MEMBERS
            metadata: dict[str, tuple[int, str]] = {}
            for name in sorted(names):
                local = remote_dir / name
                _download_remote(config_dir, f"{group_root}/{run_id}/{name}", local)
                metadata[name] = (local.stat().st_size, _sha256_file(local).hex())
            _, complete_hash = _validate_snapshot(remote_dir, run_id, names, metadata)
            lease_value = lease.read_bytes()
            if len(lease_value) != 32:
                raise ProviderFailure()
            _write_receipt(receipt, {
                "schema_version": 1,
                "status": "committed",
                "backup_run_id": run_id,
                "complete_sha256": complete_hash,
                "lease_id_hash": hashlib.sha256(lease_value).hexdigest(),
            })
        except Exception as error:
            raise CommitUnknown() from error


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parse_args(list(sys.argv[1:] if argv is None else argv))
        if arguments["operation"] == "publish-complete-group":
            _publish(arguments)
        else:
            _reconcile(arguments)
        return 0
    except LeaseConflict:
        print("publisher conflict", file=sys.stderr)
        return EXIT_CONFLICT
    except CommitUnknown:
        print("publisher commit status unknown; use read-only reconciliation", file=sys.stderr)
        return EXIT_COMMIT_UNKNOWN
    except SafetyError:
        print("publisher rejected unsafe input", file=sys.stderr)
        return EXIT_SAFETY
    except ProviderFailure:
        print("publisher provider operation failed", file=sys.stderr)
        return EXIT_PROVIDER_FAILURE
    except Exception:
        print("publisher internal failure", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
