#!/usr/bin/env python3
"""Fail-closed helpers for the Industrial Horizon modpack release workflow.

The publisher deliberately keeps network credentials out of this module.  It
validates the four GitHub release assets, creates path-safe upload metadata and
checks the public CDN after the workflow has uploaded files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from typing import BinaryIO, Iterable


CANONICAL_ASSETS = (
    "modpack.zip",
    "modpack.zip.sha256",
    "manifest.json",
    "manifest.json.sha256",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UNSAFE_BASENAME_CHARS = frozenset('<>:"/\\|?*%#')
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "Industrial-Horizon-modpack-publisher/1.0"


class ValidationError(RuntimeError):
    """A release invariant failed."""


@dataclass(frozen=True)
class JarEntry:
    name: str
    path: str
    url: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "url": self.url,
            "size": self.size,
            "sha256": self.sha256,
        }


def fail(message: str) -> None:
    raise ValidationError(message)


def sha256_stream(source: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    with path.open("rb") as source:
        return sha256_stream(source)[1]


def clean_sha(value: str, label: str) -> str:
    digest = str(value).strip().lower()
    if not SHA256_RE.fullmatch(digest):
        fail(f"{label} is not a SHA-256 digest")
    return digest


def parse_sidecar_bytes(
    payload: bytes, expected_name: str, label: str
) -> str:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is not UTF-8 text") from exc
    lines = text.splitlines()
    if len(lines) != 1:
        fail(f"{label} must contain exactly one line")
    match = re.fullmatch(r"([0-9A-Fa-f]{64})[ \t]+[*]?(.+)", lines[0])
    if not match:
        fail(f"{label} has an invalid sha256sum format")
    if match.group(2) != expected_name:
        fail(
            f"{label} names {match.group(2)!r}, expected {expected_name!r}"
        )
    return match.group(1).lower()


def parse_sidecar(path: pathlib.Path, expected_name: str) -> str:
    return parse_sidecar_bytes(path.read_bytes(), expected_name, path.name)


def is_simple_jar_basename(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(
        value
        and value == value.strip()
        and value.lower().endswith(".jar")
        and ".." not in value
        and not any(
            ord(char) < 32
            or ord(char) == 127
            or char in UNSAFE_BASENAME_CHARS
            for char in value
        )
    )


def canonical_jar_url(name: str) -> str:
    return "files/mods/" + urllib.parse.quote(name, safe="")


def is_safe_jar_url(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    parsed = urllib.parse.urlsplit(value)
    prefix = "files/mods/"
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != value
        or "\\" in value
        or not value.startswith(prefix)
    ):
        return False
    encoded_name = value[len(prefix) :]
    if not encoded_name or "/" in encoded_name:
        return False
    try:
        decoded_name = urllib.parse.unquote(encoded_name, errors="strict")
    except UnicodeError:
        return False
    return is_simple_jar_basename(decoded_name)


def validate_manifest(
    raw: object,
    expected_version: int | None = None,
    *,
    require_canonical_urls: bool = False,
) -> tuple[dict, tuple[JarEntry, ...]]:
    if not isinstance(raw, dict):
        fail("manifest.json must contain a JSON object")
    if raw.get("modsOnly") is not True:
        fail("manifest.json must set modsOnly=true")
    version = raw.get("version")
    if isinstance(version, bool):
        fail("manifest version must be an integer")
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValidationError("manifest version must be an integer") from exc
    if version <= 0:
        fail("manifest version must be positive")
    if expected_version is not None and version != expected_version:
        fail(
            f"manifest version {version} does not match requested "
            f"version {expected_version}"
        )

    raw_files = raw.get("files")
    if not isinstance(raw_files, list) or len(raw_files) < 20:
        fail("manifest files must contain at least 20 entries")

    entries: list[JarEntry] = []
    names: set[str] = set()
    urls: set[str] = set()
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            fail(f"manifest files[{index}] is not an object")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("mods/"):
            fail(f"manifest files[{index}] has an invalid path")
        name = path[5:]
        if path != f"mods/{name}" or not is_simple_jar_basename(name):
            fail(f"manifest files[{index}] has an unsafe JAR path")
        folded = name.casefold()
        if folded in names:
            fail(f"duplicate case-insensitive JAR name: {name}")
        names.add(folded)

        size = item.get("size")
        if isinstance(size, bool):
            fail(f"manifest files[{index}] has an invalid size")
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"manifest files[{index}] has an invalid size"
            ) from exc
        if size <= 0:
            fail(f"manifest files[{index}] size must be positive")

        digest = clean_sha(
            item.get("sha256", ""), f"manifest files[{index}].sha256"
        )
        expected_url = canonical_jar_url(name)
        url = item.get("url")
        if not is_safe_jar_url(url):
            fail(f"manifest files[{index}] URL is unsafe: {url!r}")
        if require_canonical_urls and url != expected_url:
            fail(
                f"manifest files[{index}] URL is not canonical: "
                f"{url!r} != {expected_url!r}"
            )
        if url.casefold() in urls:
            fail(f"duplicate case-insensitive JAR URL: {url}")
        urls.add(url.casefold())
        entries.append(
            JarEntry(
                name=name,
                path=path,
                url=url,
                size=size,
                sha256=digest,
            )
        )

    deleted = raw.get("deletedFiles", [])
    if not isinstance(deleted, list):
        fail("manifest deletedFiles must be a list")
    deleted_names: set[str] = set()
    for index, path in enumerate(deleted):
        if not isinstance(path, str) or not path.startswith("mods/"):
            fail(f"manifest deletedFiles[{index}] is invalid")
        name = path[5:]
        if (
            path != f"mods/{name}"
            or not name
            or name != name.strip()
            or "/" in name
            or "\\" in name
            or ".." in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            fail(f"manifest deletedFiles[{index}] is unsafe")
        folded = name.casefold()
        if folded in names or folded in deleted_names:
            fail(f"manifest deletedFiles overlaps or duplicates {name}")
        deleted_names.add(folded)

    return raw, tuple(entries)


def load_manifest(
    path: pathlib.Path,
    expected_version: int | None = None,
    *,
    require_canonical_urls: bool = False,
) -> tuple[dict, tuple[JarEntry, ...]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot parse {path}") from exc
    return validate_manifest(
        raw,
        expected_version,
        require_canonical_urls=require_canonical_urls,
    )


def validate_release_assets(
    deploy: pathlib.Path,
    version: int,
    expected_modpack_sha: str,
    expected_manifest_sha: str,
) -> dict[str, object]:
    expected_modpack_sha = clean_sha(
        expected_modpack_sha, "expected modpack SHA-256"
    )
    expected_manifest_sha = clean_sha(
        expected_manifest_sha, "expected manifest SHA-256"
    )
    if version <= 0:
        fail("requested version must be positive")
    for name in CANONICAL_ASSETS:
        path = deploy / name
        if not path.is_file():
            fail(f"missing canonical GitHub release asset: {name}")

    modpack = deploy / "modpack.zip"
    manifest_path = deploy / "manifest.json"
    actual_modpack_sha = sha256_file(modpack)
    actual_manifest_sha = sha256_file(manifest_path)
    if actual_modpack_sha != expected_modpack_sha:
        fail("modpack.zip does not match the workflow input SHA-256")
    if actual_manifest_sha != expected_manifest_sha:
        fail("manifest.json does not match the workflow input SHA-256")
    if parse_sidecar(
        deploy / "modpack.zip.sha256", "modpack.zip"
    ) != expected_modpack_sha:
        fail("modpack.zip.sha256 does not match the workflow input")
    if parse_sidecar(
        deploy / "manifest.json.sha256", "manifest.json"
    ) != expected_manifest_sha:
        fail("manifest.json.sha256 does not match the workflow input")

    manifest, entries = load_manifest(
        manifest_path, version, require_canonical_urls=True
    )
    archive_names: set[str] = set()
    archive_casefolded: set[str] = set()
    extracted_root = deploy / "extracted" / "mods"
    if extracted_root.exists():
        shutil.rmtree(extracted_root)
    extracted_root.mkdir(parents=True)

    expected_by_path = {entry.path: entry for entry in entries}
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(modpack) as archive:
            bad_crc = archive.testzip()
            if bad_crc is not None:
                fail(f"modpack ZIP CRC failure in {bad_crc}")
            for info in archive.infolist():
                name = info.filename
                pure = pathlib.PurePosixPath(name)
                unix_mode = info.external_attr >> 16
                if (
                    info.is_dir()
                    or pure.is_absolute()
                    or "\\" in name
                    or name != str(pure)
                    or any(part in ("", ".", "..") for part in pure.parts)
                    or name not in expected_by_path
                    or stat.S_ISLNK(unix_mode)
                ):
                    fail(f"unsafe or unexpected modpack ZIP entry: {name!r}")
                if name in archive_names or name.casefold() in archive_casefolded:
                    fail(f"duplicate modpack ZIP entry: {name}")
                archive_names.add(name)
                archive_casefolded.add(name.casefold())

                expected = expected_by_path[name]
                if info.file_size != expected.size:
                    fail(f"ZIP size mismatch for {name}")
                destination = extracted_root / expected.name
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, destination.open("wb") as out:
                    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
                if size != expected.size or digest.hexdigest() != expected.sha256:
                    fail(f"ZIP content mismatch for {name}")
                total_uncompressed += size
    except zipfile.BadZipFile as exc:
        raise ValidationError("modpack.zip is not a valid ZIP archive") from exc

    expected_names = set(expected_by_path)
    if archive_names != expected_names:
        missing = sorted(expected_names - archive_names)
        extra = sorted(archive_names - expected_names)
        fail(f"ZIP/manifest entry mismatch; missing={missing}, extra={extra}")

    sidecars_root = deploy / "jar-sidecars"
    if sidecars_root.exists():
        shutil.rmtree(sidecars_root)
    sidecars_root.mkdir()
    for entry in entries:
        (sidecars_root / f"{entry.name}.sha256").write_text(
            f"{entry.sha256}  {entry.name}\n", encoding="ascii"
        )

    (deploy / "modpack_version.txt").write_text(
        f"{version}\n", encoding="ascii"
    )
    metadata = {
        "version": version,
        "modpack_sha256": expected_modpack_sha,
        "manifest_sha256": expected_manifest_sha,
        "modpack_size": modpack.stat().st_size,
        "manifest_size": manifest_path.stat().st_size,
        "jar_count": len(entries),
        "jar_bytes": total_uncompressed,
        "jars": [entry.as_dict() for entry in entries],
    }
    (deploy / "validated.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Preserve the parsed document for collision checks without re-reading a
    # potentially replaced source file later in the job.
    (deploy / "validated-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return metadata


def snapshot_release_assets(
    release_json: pathlib.Path, output: pathlib.Path, expected_tag: str
) -> dict[str, object]:
    try:
        release = json.loads(release_json.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot parse GitHub release metadata") from exc
    if release.get("tag_name") != expected_tag:
        fail(
            "GitHub release metadata is not for the expected staging tag"
        )
    if release.get("draft") is not False or release.get("prerelease") is not True:
        fail("staging release must be a published prerelease, not a draft")
    assets = release.get("assets")
    if not isinstance(assets, list):
        fail("GitHub release metadata has no asset list")
    selected: list[dict[str, object]] = []
    for name in CANONICAL_ASSETS:
        matches = [asset for asset in assets if asset.get("name") == name]
        if len(matches) != 1:
            fail(f"GitHub release must have exactly one {name} asset")
        asset = matches[0]
        if (
            not isinstance(asset.get("id"), int)
            or not isinstance(asset.get("size"), int)
            or asset["size"] <= 0
            or not isinstance(asset.get("updated_at"), str)
        ):
            fail(f"GitHub release metadata for {name} is incomplete")
        selected.append(
            {
                "id": asset["id"],
                "name": name,
                "size": asset["size"],
                "updated_at": asset["updated_at"],
                "digest": asset.get("digest"),
            }
        )
    snapshot = {
        "release_id": release.get("id"),
        "tag": expected_tag,
        "assets": selected,
    }
    output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return snapshot


def compare_release_snapshots(left: pathlib.Path, right: pathlib.Path) -> None:
    try:
        first = json.loads(left.read_text(encoding="utf-8"))
        second = json.loads(right.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot parse release snapshot") from exc
    if first != second:
        fail(
            "the mutable GitHub modpack release changed during publication; "
            "refusing activation"
        )


def file_hashes(root: pathlib.Path, names: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        path = root / name
        if not path.is_file():
            fail(f"receipt input is missing {name}")
        result[name] = sha256_file(path)
    return result


def make_prepared_receipt(
    metadata_path: pathlib.Path,
    staging_snapshot_path: pathlib.Path,
    channel_snapshot_path: pathlib.Path,
    fallback_root: pathlib.Path,
    backend: str,
    run_id: str,
    staging_name: str,
    output: pathlib.Path,
) -> str:
    if backend not in {"bunny", "sftp"}:
        fail("invalid receipt backend")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    staging_snapshot = json.loads(
        staging_snapshot_path.read_text(encoding="utf-8")
    )
    channel_snapshot = json.loads(
        channel_snapshot_path.read_text(encoding="utf-8")
    )
    fallback_names = (
        "modpack.zip",
        "modpack.zip.sha256",
        "manifest.json",
        "manifest.json.sha256",
        "modpack_version.txt",
    )
    receipt = {
        "schema": 1,
        "kind": "industrial-horizon-modpack-prepared",
        "version": metadata["version"],
        "modpack_sha256": metadata["modpack_sha256"],
        "manifest_sha256": metadata["manifest_sha256"],
        "modpack_size": metadata["modpack_size"],
        "jar_count": metadata["jar_count"],
        "backend": backend,
        "prepare_run_id": str(run_id),
        "staging_name": staging_name,
        "previous_version": channel_snapshot["current_version"],
        "previous_public_manifest_sha256": channel_snapshot[
            "active_manifest_sha256"
        ],
        "staging_snapshot": staging_snapshot,
        "github_fallback_sha256": file_hashes(
            fallback_root, fallback_names
        ),
    }
    output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = sha256_file(output)
    output.with_name(output.name + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )
    return digest


def validate_prepared_receipt(
    receipt_path: pathlib.Path,
    sidecar_path: pathlib.Path,
    expected_receipt_sha: str,
    metadata_path: pathlib.Path,
    staging_snapshot_path: pathlib.Path,
    backend: str,
    staging_name: str,
) -> dict[str, object]:
    expected_receipt_sha = clean_sha(
        expected_receipt_sha, "expected prepared receipt SHA-256"
    )
    if sha256_file(receipt_path) != expected_receipt_sha:
        fail("prepared receipt does not match the activation input SHA-256")
    if (
        parse_sidecar(sidecar_path, receipt_path.name)
        != expected_receipt_sha
    ):
        fail("prepared receipt sidecar does not match")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    snapshot = json.loads(staging_snapshot_path.read_text(encoding="utf-8"))
    if (
        receipt.get("schema") != 1
        or receipt.get("kind")
        != "industrial-horizon-modpack-prepared"
        or receipt.get("version") != metadata.get("version")
        or receipt.get("modpack_sha256") != metadata.get("modpack_sha256")
        or receipt.get("manifest_sha256") != metadata.get("manifest_sha256")
        or receipt.get("modpack_size") != metadata.get("modpack_size")
        or receipt.get("jar_count") != metadata.get("jar_count")
        or receipt.get("backend") != backend
        or receipt.get("staging_name") != staging_name
        or receipt.get("staging_snapshot") != snapshot
    ):
        fail("prepared receipt does not match this activation")
    previous = receipt.get("previous_version")
    if isinstance(previous, bool) or not isinstance(previous, int):
        fail("prepared receipt has an invalid previous version")
    fallback_hashes = receipt.get("github_fallback_sha256")
    if not isinstance(fallback_hashes, dict) or len(fallback_hashes) != 5:
        fail("prepared receipt has invalid fallback hashes")
    for name, digest in fallback_hashes.items():
        if not isinstance(name, str):
            fail("prepared receipt has an invalid fallback name")
        clean_sha(digest, f"prepared fallback digest for {name}")
    return receipt


def check_fallback_receipt(
    receipt_path: pathlib.Path, fallback_root: pathlib.Path
) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected = receipt.get("github_fallback_sha256")
    if not isinstance(expected, dict):
        fail("prepared receipt has no fallback snapshot")
    actual = file_hashes(fallback_root, expected.keys())
    if actual != expected:
        fail("live GitHub fallback changed after PREPARE")


def preflight_active_channel(
    marker_path: pathlib.Path,
    active_manifest_path: pathlib.Path,
    new_manifest_path: pathlib.Path,
    requested_version: int,
    expected_manifest_sha: str,
    output: pathlib.Path,
) -> dict[str, object]:
    try:
        marker_text = marker_path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ValidationError("cannot read current public marker") from exc
    if not re.fullmatch(r"[0-9]+", marker_text):
        fail("current public modpack marker is not an integer")
    current_version = int(marker_text)
    if current_version > requested_version:
        fail(
            f"refusing modpack downgrade from {current_version} "
            f"to {requested_version}"
        )

    _, active_entries = load_manifest(active_manifest_path, current_version)
    _, new_entries = load_manifest(new_manifest_path, requested_version)
    active_by_url = {entry.url: entry for entry in active_entries}
    collisions = []
    for entry in new_entries:
        previous = active_by_url.get(entry.url)
        if previous and (
            previous.sha256 != entry.sha256 or previous.size != entry.size
        ):
            collisions.append(entry.url)
    if collisions:
        fail(
            "new manifest mutates an active immutable JAR URL: "
            + ", ".join(collisions)
        )

    active_manifest_sha = sha256_file(active_manifest_path)
    expected_manifest_sha = clean_sha(
        expected_manifest_sha, "expected manifest SHA-256"
    )
    if (
        current_version == requested_version
        and active_manifest_sha != expected_manifest_sha
    ):
        fail(
            "same-version publication would replace the active manifest; "
            "bump the modpack version"
        )
    snapshot = {
        "current_version": current_version,
        "active_manifest_sha256": active_manifest_sha,
        "requested_version": requested_version,
    }
    output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return snapshot


def validate_remote_root(value: str) -> str:
    if not value or value != value.strip() or not value.startswith("/"):
        fail("REMOTE_DIR must be an absolute SFTP path")
    if any(ord(char) < 32 or char in {'"', "\\"} for char in value):
        fail("REMOTE_DIR contains unsupported characters")
    parts = [part for part in value.split("/") if part]
    if any(part in (".", "..") for part in parts):
        fail("REMOTE_DIR contains traversal")
    cleaned = value.rstrip("/")
    if not cleaned:
        fail("REMOTE_DIR may not be the SFTP account root")
    return cleaned


def sftp_quote(value: str) -> str:
    if '"' in value or "\r" in value or "\n" in value:
        fail("cannot safely quote SFTP path")
    return f'"{value}"'


def make_sftp_batch(
    metadata_path: pathlib.Path,
    remote_root: str,
    output: pathlib.Path,
    kind: str,
    staging_name: str,
    rollback_name: str,
) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    remote_root = validate_remote_root(remote_root)
    lines: list[str] = []
    staging_root = f"{remote_root}/_staging/{staging_name}"
    rollback_root = f"{remote_root}/_rollback/{rollback_name}"
    payload_names = (
        "modpack.zip",
        "modpack.zip.sha256",
        "manifest.json",
        "manifest.json.sha256",
    )
    if kind == "jars":
        lines.extend(
            (
                f"-mkdir {sftp_quote(remote_root + '/files')}",
                f"-mkdir {sftp_quote(remote_root + '/files/mods')}",
            )
        )
        for item in metadata["jars"]:
            name = item["name"]
            lines.append(
                "put "
                + sftp_quote(f"deploy/extracted/mods/{name}")
                + " "
                + sftp_quote(f"{remote_root}/files/mods/{name}")
            )
            lines.append(
                "put "
                + sftp_quote(f"deploy/jar-sidecars/{name}.sha256")
                + " "
                + sftp_quote(f"{remote_root}/files/mods/{name}.sha256")
            )
    elif kind == "payload":
        lines.extend(
            (
                f"-mkdir {sftp_quote(remote_root + '/_staging')}",
                f"-mkdir {sftp_quote(staging_root)}",
            )
        )
        for name in (*payload_names, "modpack_version.txt"):
            lines.append(
                "put "
                + sftp_quote(f"deploy/{name}")
                + " "
                + sftp_quote(f"{staging_root}/{name}")
            )
    elif kind == "activate-payload":
        lines.extend(
            (
                f"-mkdir {sftp_quote(remote_root + '/_rollback')}",
                f"-mkdir {sftp_quote(rollback_root)}",
            )
        )
        for name in payload_names:
            lines.append(
                "rename "
                + sftp_quote(f"{remote_root}/{name}")
                + " "
                + sftp_quote(f"{rollback_root}/{name}")
            )
        for name in payload_names:
            lines.append(
                "rename "
                + sftp_quote(f"{staging_root}/{name}")
                + " "
                + sftp_quote(f"{remote_root}/{name}")
            )
    elif kind == "activate-marker":
        lines.append(
            "rename "
            + sftp_quote(f"{remote_root}/modpack_version.txt")
            + " "
            + sftp_quote(f"{rollback_root}/modpack_version.txt")
        )
        lines.append(
            "rename "
            + sftp_quote(f"{staging_root}/modpack_version.txt")
            + " "
            + sftp_quote(f"{remote_root}/modpack_version.txt")
        )
    elif kind == "rollback":
        for name in (*payload_names, "modpack_version.txt"):
            lines.append(
                f"-rm {sftp_quote(f'{remote_root}/{name}')}"
            )
            lines.append(
                "-rename "
                + sftp_quote(f"{rollback_root}/{name}")
                + " "
                + sftp_quote(f"{remote_root}/{name}")
            )
    elif kind == "restore":
        for name in (*payload_names, "modpack_version.txt"):
            lines.append(
                "put "
                + sftp_quote(f"rollback/cdn/{name}")
                + " "
                + sftp_quote(f"{remote_root}/{name}")
            )
    else:
        fail(f"unsupported SFTP batch kind: {kind}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_cache_buster(url: str, token: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}ihverify={urllib.parse.quote(token, safe='')}"


def open_url(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    attempts: int = 4,
):
    merged_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        **(headers or {}),
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url, method=method, headers=merged_headers
        )
        try:
            return urllib.request.urlopen(request, timeout=90)
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2**attempt, 8))
    raise ValidationError(f"public request failed for {url}") from last_error


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    limit: int | None = None,
) -> bytes:
    with open_url(url, headers=headers) as response:
        data = response.read() if limit is None else response.read(limit + 1)
    if limit is not None and len(data) > limit:
        fail(f"public response exceeded {limit} bytes: {url}")
    return data


def public_file_hash(
    url: str, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    with open_url(url, headers=headers) as response:
        return sha256_stream(response)


def verify_one_public_jar(
    entry: dict[str, object],
    extracted_root: pathlib.Path,
    public_base: str,
    token: str,
) -> str:
    name = str(entry["name"])
    size = int(entry["size"])
    digest = str(entry["sha256"])
    relative_url = str(entry["url"])
    _ = token
    no_cache = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    jar_url = f"{public_base.rstrip('/')}/{relative_url}"
    with open_url(
        jar_url, method="HEAD", headers=no_cache
    ) as response:
        if response.status != 200:
            fail(f"HEAD returned {response.status} for {name}")
        try:
            remote_size = int(response.headers["Content-Length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"HEAD has no valid size for {name}") from exc
    if remote_size != size:
        fail(f"HEAD size mismatch for {name}")

    local = extracted_root / name
    spot_size = min(4096, size)
    with local.open("rb") as source:
        expected_first = source.read(spot_size)
        source.seek(size - spot_size)
        expected_last = source.read(spot_size)
    ranges = (
        (0, spot_size - 1, expected_first, "first"),
        (size - spot_size, size - 1, expected_last, "last"),
    )
    for start, end, expected, label in ranges:
        with open_url(
            jar_url,
            headers={
                **no_cache,
                "Range": f"bytes={start}-{end}",
            },
        ) as response:
            if response.status != 206:
                fail(f"range request returned {response.status} for {name}")
            actual = response.read(spot_size + 1)
        if actual != expected:
            fail(f"{label} range mismatch for {name}")

    sidecar_url = f"{public_base.rstrip('/')}/{relative_url}.sha256"
    remote_sidecar = parse_sidecar_bytes(
        fetch_bytes(sidecar_url, headers=no_cache, limit=512),
        name,
        f"public sidecar for {name}",
    )
    if remote_sidecar != digest:
        fail(f"public sidecar mismatch for {name}")

    remote_size, remote_sha = public_file_hash(
        jar_url, headers=no_cache
    )
    if remote_size != size or remote_sha != digest:
        fail(f"full public hash mismatch for {name}")
    return name


def verify_public_jars(
    metadata_path: pathlib.Path,
    extracted_root: pathlib.Path,
    public_base: str,
    token: str,
    workers: int,
) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    entries = metadata["jars"]
    if workers < 1 or workers > 8:
        fail("public JAR verification workers must be between 1 and 8")
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        futures = {
            executor.submit(
                verify_one_public_jar,
                entry,
                extracted_root,
                public_base,
                token,
            ): entry["name"]
            for entry in entries
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as exc:
                raise ValidationError(
                    f"public JAR verification failed for {name}: {exc}"
                ) from exc
            completed += 1
            if completed % 10 == 0 or completed == len(entries):
                print(f"Verified public JARs: {completed}/{len(entries)}")


def verify_public_payload(
    metadata_path: pathlib.Path,
    public_base: str,
    token: str,
    attempts: int,
    interval: int,
    include_zip: bool,
) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_manifest = metadata["manifest_sha256"]
    expected_modpack = metadata["modpack_sha256"]
    expected_modpack_size = int(metadata["modpack_size"])
    last_error: Exception | None = None
    _ = token
    for attempt in range(1, attempts + 1):
        no_cache = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        try:
            manifest_url = f"{public_base.rstrip('/')}/manifest.json"
            manifest_bytes = fetch_bytes(
                manifest_url,
                headers=no_cache,
                limit=2 * 1024 * 1024,
            )
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            if manifest_sha != expected_manifest:
                fail("public manifest.json SHA-256 mismatch")
            public_manifest, _ = validate_manifest(
                json.loads(manifest_bytes.decode("utf-8")),
                int(metadata["version"]),
            )
            del public_manifest

            manifest_sidecar = parse_sidecar_bytes(
                fetch_bytes(
                    f"{public_base.rstrip('/')}/manifest.json.sha256",
                    headers=no_cache,
                    limit=512,
                ),
                "manifest.json",
                "public manifest sidecar",
            )
            modpack_sidecar = parse_sidecar_bytes(
                fetch_bytes(
                    f"{public_base.rstrip('/')}/modpack.zip.sha256",
                    headers=no_cache,
                    limit=512,
                ),
                "modpack.zip",
                "public modpack sidecar",
            )
            if (
                manifest_sidecar != expected_manifest
                or modpack_sidecar != expected_modpack
            ):
                fail("public payload sidecar mismatch")

            if include_zip:
                zip_url = f"{public_base.rstrip('/')}/modpack.zip"
                with open_url(
                    zip_url, method="HEAD", headers=no_cache
                ) as response:
                    try:
                        head_size = int(response.headers["Content-Length"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValidationError(
                            "public modpack.zip HEAD has no valid size"
                        ) from exc
                if head_size != expected_modpack_size:
                    fail("public modpack.zip HEAD size mismatch")
                remote_size, remote_sha = public_file_hash(
                    zip_url, headers=no_cache
                )
                if (
                    remote_size != expected_modpack_size
                    or remote_sha != expected_modpack
                ):
                    fail("public modpack.zip full hash mismatch")
            return
        except (
            ValidationError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt < attempts:
                print(
                    f"Public payload is not coherent yet "
                    f"({attempt}/{attempts}); retrying."
                )
                time.sleep(interval)
    raise ValidationError(
        f"public payload did not become coherent: {last_error}"
    )


def read_public_marker(
    public_base: str,
    token: str,
    expected: int | None,
    attempts: int = 1,
    interval: int = 15,
) -> int:
    last_value: int | None = None
    _ = token
    for attempt in range(1, attempts + 1):
        payload = fetch_bytes(
            f"{public_base.rstrip('/')}/modpack_version.txt",
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            limit=128,
        )
        try:
            text = payload.decode("utf-8-sig").strip()
        except UnicodeDecodeError as exc:
            raise ValidationError("public marker is not UTF-8") from exc
        if not re.fullmatch(r"[0-9]+", text):
            fail("public marker is not an integer")
        last_value = int(text)
        if expected is None or last_value == expected:
            return last_value
        if attempt < attempts:
            print(
                f"Public marker is still {last_value}; waiting for "
                f"{expected} ({attempt}/{attempts})."
            )
            time.sleep(interval)
    fail(f"public marker is {last_value}, expected {expected}")


def prepare_sftp_preflight(
    public_manifest: pathlib.Path,
    remote_root: str,
    output: pathlib.Path,
) -> None:
    _, entries = load_manifest(public_manifest)
    remote_root = validate_remote_root(remote_root)
    lines = [
        "get "
        + sftp_quote(f"{remote_root}/modpack_version.txt")
        + " "
        + sftp_quote("origin/modpack_version.txt"),
        "get "
        + sftp_quote(f"{remote_root}/manifest.json")
        + " "
        + sftp_quote("origin/manifest.json"),
        "get "
        + sftp_quote(f"{remote_root}/manifest.json.sha256")
        + " "
        + sftp_quote("origin/manifest.json.sha256"),
        "get "
        + sftp_quote(f"{remote_root}/modpack.zip.sha256")
        + " "
        + sftp_quote("origin/modpack.zip.sha256"),
    ]
    for index, entry in enumerate(entries):
        lines.append(
            "get "
            + sftp_quote(
                f"{remote_root}/files/mods/{entry.name}.sha256"
            )
            + " "
            + sftp_quote(f"origin/jar-sidecars/{index:04d}.sha256")
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_sftp_preflight(
    public_dir: pathlib.Path, origin_dir: pathlib.Path
) -> None:
    public_marker = (
        public_dir / "modpack_version.txt"
    ).read_text(encoding="utf-8-sig").strip()
    origin_marker = (
        origin_dir / "modpack_version.txt"
    ).read_text(encoding="utf-8-sig").strip()
    if public_marker != origin_marker or not public_marker.isdigit():
        fail("SFTP origin and public modpack markers differ")

    public_manifest_path = public_dir / "manifest.json"
    origin_manifest_path = origin_dir / "manifest.json"
    public_manifest, public_entries = load_manifest(
        public_manifest_path, int(public_marker)
    )
    origin_manifest, origin_entries = load_manifest(
        origin_manifest_path, int(public_marker)
    )
    if public_manifest != origin_manifest or public_entries != origin_entries:
        fail("SFTP origin and public manifests differ")
    expected_manifest_sha = sha256_file(public_manifest_path)
    for directory, label in (
        (public_dir, "public"),
        (origin_dir, "origin"),
    ):
        if parse_sidecar(
            directory / "manifest.json.sha256", "manifest.json"
        ) != expected_manifest_sha:
            fail(f"{label} manifest sidecar mismatch")
    public_modpack_sha = parse_sidecar(
        public_dir / "modpack.zip.sha256", "modpack.zip"
    )
    origin_modpack_sha = parse_sidecar(
        origin_dir / "modpack.zip.sha256", "modpack.zip"
    )
    if public_modpack_sha != origin_modpack_sha:
        fail("SFTP origin and public modpack sidecars differ")

    sidecars = sorted((origin_dir / "jar-sidecars").glob("*.sha256"))
    if len(sidecars) != len(public_entries):
        fail("SFTP origin JAR sidecar count does not match manifest")
    for index, (entry, path) in enumerate(zip(public_entries, sidecars)):
        digest = parse_sidecar(path, entry.name)
        if digest != entry.sha256:
            fail(f"SFTP origin JAR sidecar mismatch at index {index}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--deploy", type=pathlib.Path, required=True)
    validate.add_argument("--version", type=int, required=True)
    validate.add_argument("--expected-modpack-sha", required=True)
    validate.add_argument("--expected-manifest-sha", required=True)

    snapshot = sub.add_parser("snapshot-release")
    snapshot.add_argument("--release-json", type=pathlib.Path, required=True)
    snapshot.add_argument("--output", type=pathlib.Path, required=True)
    snapshot.add_argument("--expected-tag", required=True)

    compare = sub.add_parser("compare-snapshots")
    compare.add_argument("--first", type=pathlib.Path, required=True)
    compare.add_argument("--second", type=pathlib.Path, required=True)

    receipt = sub.add_parser("make-receipt")
    receipt.add_argument("--metadata", type=pathlib.Path, required=True)
    receipt.add_argument(
        "--staging-snapshot", type=pathlib.Path, required=True
    )
    receipt.add_argument(
        "--channel-snapshot", type=pathlib.Path, required=True
    )
    receipt.add_argument(
        "--fallback-root", type=pathlib.Path, required=True
    )
    receipt.add_argument("--backend", required=True)
    receipt.add_argument("--run-id", required=True)
    receipt.add_argument("--staging-name", required=True)
    receipt.add_argument("--output", type=pathlib.Path, required=True)

    validate_receipt = sub.add_parser("validate-receipt")
    validate_receipt.add_argument(
        "--receipt", type=pathlib.Path, required=True
    )
    validate_receipt.add_argument(
        "--sidecar", type=pathlib.Path, required=True
    )
    validate_receipt.add_argument("--expected-sha", required=True)
    validate_receipt.add_argument(
        "--metadata", type=pathlib.Path, required=True
    )
    validate_receipt.add_argument(
        "--staging-snapshot", type=pathlib.Path, required=True
    )
    validate_receipt.add_argument("--backend", required=True)
    validate_receipt.add_argument("--staging-name", required=True)

    check_fallback = sub.add_parser("check-fallback-receipt")
    check_fallback.add_argument(
        "--receipt", type=pathlib.Path, required=True
    )
    check_fallback.add_argument(
        "--fallback-root", type=pathlib.Path, required=True
    )

    active = sub.add_parser("preflight-active")
    active.add_argument("--marker", type=pathlib.Path, required=True)
    active.add_argument("--active-manifest", type=pathlib.Path, required=True)
    active.add_argument("--new-manifest", type=pathlib.Path, required=True)
    active.add_argument("--version", type=int, required=True)
    active.add_argument("--expected-manifest-sha", required=True)
    active.add_argument("--output", type=pathlib.Path, required=True)

    batch = sub.add_parser("make-sftp-batch")
    batch.add_argument("--metadata", type=pathlib.Path, required=True)
    batch.add_argument("--remote-root", required=True)
    batch.add_argument("--output", type=pathlib.Path, required=True)
    batch.add_argument(
        "--kind",
        required=True,
        choices=(
            "jars",
            "payload",
            "activate-payload",
            "activate-marker",
            "rollback",
            "restore",
        ),
    )
    batch.add_argument("--staging-name", required=True)
    batch.add_argument("--rollback-name", required=True)

    verify_jars = sub.add_parser("verify-public-jars")
    verify_jars.add_argument("--metadata", type=pathlib.Path, required=True)
    verify_jars.add_argument(
        "--extracted-root", type=pathlib.Path, required=True
    )
    verify_jars.add_argument("--public-base", required=True)
    verify_jars.add_argument("--token", required=True)
    verify_jars.add_argument("--workers", type=int, default=4)

    verify_payload = sub.add_parser("verify-public-payload")
    verify_payload.add_argument(
        "--metadata", type=pathlib.Path, required=True
    )
    verify_payload.add_argument("--public-base", required=True)
    verify_payload.add_argument("--token", required=True)
    verify_payload.add_argument("--attempts", type=int, default=1)
    verify_payload.add_argument("--interval", type=int, default=15)
    verify_payload.add_argument("--include-zip", action="store_true")

    marker = sub.add_parser("read-public-marker")
    marker.add_argument("--public-base", required=True)
    marker.add_argument("--token", required=True)
    marker.add_argument("--expected", type=int)
    marker.add_argument("--attempts", type=int, default=1)
    marker.add_argument("--interval", type=int, default=15)

    prepare_preflight = sub.add_parser("prepare-sftp-preflight")
    prepare_preflight.add_argument(
        "--public-manifest", type=pathlib.Path, required=True
    )
    prepare_preflight.add_argument("--remote-root", required=True)
    prepare_preflight.add_argument(
        "--output", type=pathlib.Path, required=True
    )

    verify_preflight = sub.add_parser("verify-sftp-preflight")
    verify_preflight.add_argument(
        "--public-dir", type=pathlib.Path, required=True
    )
    verify_preflight.add_argument(
        "--origin-dir", type=pathlib.Path, required=True
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            metadata = validate_release_assets(
                args.deploy,
                args.version,
                args.expected_modpack_sha,
                args.expected_manifest_sha,
            )
            print(
                f"Validated modpack v{metadata['version']}: "
                f"{metadata['jar_count']} JARs"
            )
        elif args.command == "snapshot-release":
            snapshot_release_assets(
                args.release_json, args.output, args.expected_tag
            )
        elif args.command == "compare-snapshots":
            compare_release_snapshots(args.first, args.second)
        elif args.command == "make-receipt":
            digest = make_prepared_receipt(
                args.metadata,
                args.staging_snapshot,
                args.channel_snapshot,
                args.fallback_root,
                args.backend,
                args.run_id,
                args.staging_name,
                args.output,
            )
            print(digest)
        elif args.command == "validate-receipt":
            validate_prepared_receipt(
                args.receipt,
                args.sidecar,
                args.expected_sha,
                args.metadata,
                args.staging_snapshot,
                args.backend,
                args.staging_name,
            )
        elif args.command == "check-fallback-receipt":
            check_fallback_receipt(args.receipt, args.fallback_root)
        elif args.command == "preflight-active":
            preflight_active_channel(
                args.marker,
                args.active_manifest,
                args.new_manifest,
                args.version,
                args.expected_manifest_sha,
                args.output,
            )
        elif args.command == "make-sftp-batch":
            make_sftp_batch(
                args.metadata,
                args.remote_root,
                args.output,
                args.kind,
                args.staging_name,
                args.rollback_name,
            )
        elif args.command == "verify-public-jars":
            verify_public_jars(
                args.metadata,
                args.extracted_root,
                args.public_base,
                args.token,
                args.workers,
            )
        elif args.command == "verify-public-payload":
            verify_public_payload(
                args.metadata,
                args.public_base,
                args.token,
                args.attempts,
                args.interval,
                args.include_zip,
            )
        elif args.command == "read-public-marker":
            value = read_public_marker(
                args.public_base,
                args.token,
                args.expected,
                args.attempts,
                args.interval,
            )
            print(value)
        elif args.command == "prepare-sftp-preflight":
            prepare_sftp_preflight(
                args.public_manifest, args.remote_root, args.output
            )
        elif args.command == "verify-sftp-preflight":
            verify_sftp_preflight(args.public_dir, args.origin_dir)
        else:  # pragma: no cover - argparse enforces this
            fail(f"unsupported command: {args.command}")
    except ValidationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
