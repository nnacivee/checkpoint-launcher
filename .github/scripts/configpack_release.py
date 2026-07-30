#!/usr/bin/env python3
"""Validation and public verification for staged configpack releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import stat
import sys
import time
import tomllib
import zipfile

from modpack_release import (
    ValidationError,
    clean_sha,
    fetch_bytes,
    open_url,
    parse_sidecar,
    parse_sidecar_bytes,
    public_file_hash,
    sha256_file,
    sftp_quote,
    validate_remote_root,
)


CANONICAL_ASSETS = ("configpack.zip", "configpack.zip.sha256")
REQUIRED_EPOCH_FILES = {
    "config/ftbquests/quests/chapters/01_create.snbt",
    "config/ftbquests/quests/chapters/02_industry.snbt",
    "config/ftbquests/quests/chapters/03_electronics.snbt",
    "config/ftbquests/quests/chapters/04_space.snbt",
    "config/progressivestages/progressivestages.toml",
    "config/progressivestages/stages/ih_ae2_networks.toml",
    "config/progressivestages/stages/ih_epoch_electronics.toml",
    "config/progressivestages/stages/ih_epoch_industry.toml",
    "config/progressivestages/stages/ih_epoch_space.toml",
    "kubejs/server_scripts/ih_15_eras.js",
    "kubejs/startup_scripts/ih_00_seals.js",
}


def fail(message: str) -> None:
    raise ValidationError(message)


def parse_configpack_manifest(raw: object, expected_version: int) -> dict:
    if not isinstance(raw, dict):
        fail("configpack.json must contain a JSON object")
    version = raw.get("version")
    if isinstance(version, bool):
        fail("configpack version must be an integer")
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValidationError("configpack version must be an integer") from exc
    if version != expected_version:
        fail(
            f"embedded configpack version {version} does not match "
            f"requested version {expected_version}"
        )
    owns = raw.get("owns")
    if not isinstance(owns, list) or not owns:
        fail("configpack owns must be a non-empty list")
    seen: set[str] = set()
    for index, value in enumerate(owns):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip("/")
            or "\\" in value
            or any(
                part in ("", ".", "..")
                for part in pathlib.PurePosixPath(value).parts
            )
        ):
            fail(f"unsafe configpack owns entry at index {index}")
        folded = value.casefold()
        if folded in seen:
            fail(f"duplicate configpack owns entry: {value}")
        seen.add(folded)
    return raw


def validate_configpack(
    deploy: pathlib.Path,
    version: int,
    expected_sha: str,
) -> dict[str, object]:
    if version <= 0:
        fail("requested configpack version must be positive")
    expected_sha = clean_sha(expected_sha, "expected configpack SHA-256")
    archive_path = deploy / "configpack.zip"
    sidecar_path = deploy / "configpack.zip.sha256"
    for path in (archive_path, sidecar_path):
        if not path.is_file():
            fail(f"missing canonical staging asset: {path.name}")
    if sha256_file(archive_path) != expected_sha:
        fail("configpack.zip does not match the workflow input SHA-256")
    if parse_sidecar(sidecar_path, "configpack.zip") != expected_sha:
        fail("configpack.zip.sha256 does not match the workflow input")

    extract = deploy / "validated-configpack"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir()
    names: set[str] = set()
    folded_names: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_crc = archive.testzip()
            if bad_crc is not None:
                fail(f"configpack ZIP CRC failure in {bad_crc}")
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
                    or stat.S_ISLNK(unix_mode)
                ):
                    fail(f"unsafe configpack ZIP entry: {name!r}")
                if name in names or name.casefold() in folded_names:
                    fail(f"duplicate configpack ZIP entry: {name}")
                names.add(name)
                folded_names.add(name.casefold())
                total_size += info.file_size
                if total_size > 1024 * 1024 * 1024:
                    fail("configpack uncompressed size exceeds 1 GiB")
                destination = extract.joinpath(*pure.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise ValidationError("configpack.zip is not a valid ZIP") from exc

    manifest_path = extract / "configpack.json"
    try:
        manifest = parse_configpack_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8")), version
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot parse embedded configpack.json") from exc

    owns = tuple(str(value) for value in manifest["owns"])
    for name in names - {"configpack.json"}:
        if not any(name == owned or name.startswith(owned + "/") for owned in owns):
            fail(f"ZIP entry is not launcher-owned: {name}")

    immediately_fast = json.loads(
        (extract / "config/immediatelyfast.json").read_text(encoding="utf-8")
    )
    if immediately_fast.get("hud_batching") is not False:
        fail("ImmediatelyFast hud_batching must be false")
    if immediately_fast.get("experimental_screen_batching") is not False:
        fail("ImmediatelyFast experimental_screen_batching must be false")

    if version >= 59:
        missing = sorted(REQUIRED_EPOCH_FILES - names)
        if missing:
            fail(f"epoch configpack files are missing: {missing}")
        progressive = tomllib.loads(
            (
                extract
                / "config/progressivestages/progressivestages.toml"
            ).read_text(encoding="utf-8")
        )
        enforcement = progressive.get("enforcement", {})
        client = progressive.get("client", {})
        # Current ProgressiveStages writes both controls below enforcement;
        # tolerate a future section move but require exact boolean values.
        combined = {**client, **enforcement, **progressive}
        if combined.get("allow_creative_bypass") is not True:
            fail("ProgressiveStages creative bypass must remain enabled")
        if combined.get("show_creative_bypass_popup") is not False:
            fail("ProgressiveStages creative popup must remain disabled")

    metadata = {
        "version": version,
        "configpack_sha256": expected_sha,
        "configpack_size": archive_path.stat().st_size,
        "entry_count": len(names),
        "uncompressed_size": total_size,
    }
    (deploy / "validated-configpack.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (deploy / "configpack_version.txt").write_text(
        f"{version}\n", encoding="ascii"
    )
    return metadata


def snapshot_release(
    release_json: pathlib.Path, expected_tag: str, output: pathlib.Path
) -> None:
    release = json.loads(release_json.read_text(encoding="utf-8-sig"))
    if release.get("tag_name") != expected_tag:
        fail("release metadata is not for the expected configpack staging tag")
    if release.get("draft") is not False or release.get("prerelease") is not True:
        fail("configpack staging release must be a published prerelease")
    assets = release.get("assets")
    if not isinstance(assets, list):
        fail("release metadata has no assets")
    selected = []
    for name in CANONICAL_ASSETS:
        matches = [asset for asset in assets if asset.get("name") == name]
        if len(matches) != 1:
            fail(f"staging release must have exactly one {name}")
        asset = matches[0]
        if (
            not isinstance(asset.get("id"), int)
            or not isinstance(asset.get("size"), int)
            or asset["size"] <= 0
            or not isinstance(asset.get("updated_at"), str)
        ):
            fail(f"staging asset metadata for {name} is incomplete")
        selected.append(
            {
                "id": asset["id"],
                "name": name,
                "size": asset["size"],
                "updated_at": asset["updated_at"],
                "digest": asset.get("digest"),
            }
        )
    output.write_text(
        json.dumps(
            {
                "release_id": release.get("id"),
                "tag": expected_tag,
                "assets": selected,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def file_hashes(
    root: pathlib.Path, names: tuple[str, ...]
) -> dict[str, str]:
    result = {}
    for name in names:
        path = root / name
        if not path.is_file():
            fail(f"receipt input is missing {name}")
        result[name] = sha256_file(path)
    return result


def preflight_active(
    marker_path: pathlib.Path,
    active_root: pathlib.Path,
    requested_version: int,
    expected_sha: str,
    output: pathlib.Path,
) -> None:
    marker = marker_path.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"[0-9]+", marker):
        fail("current configpack marker is invalid")
    current = int(marker)
    if current > requested_version:
        fail(
            f"refusing configpack downgrade from {current} "
            f"to {requested_version}"
        )
    active_sha = parse_sidecar(
        active_root / "configpack.zip.sha256", "configpack.zip"
    )
    if current == requested_version and active_sha != clean_sha(
        expected_sha, "expected configpack SHA-256"
    ):
        fail("same-version configpack publication would mutate the payload")
    output.write_text(
        json.dumps(
            {
                "current_version": current,
                "active_configpack_sha256": active_sha,
                "requested_version": requested_version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def make_receipt(
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
    snapshot = json.loads(
        staging_snapshot_path.read_text(encoding="utf-8")
    )
    channel = json.loads(
        channel_snapshot_path.read_text(encoding="utf-8")
    )
    receipt = {
        "schema": 1,
        "kind": "industrial-horizon-configpack-prepared",
        "version": metadata["version"],
        "configpack_sha256": metadata["configpack_sha256"],
        "configpack_size": metadata["configpack_size"],
        "entry_count": metadata["entry_count"],
        "backend": backend,
        "prepare_run_id": str(run_id),
        "staging_name": staging_name,
        "previous_version": channel["current_version"],
        "previous_configpack_sha256": channel[
            "active_configpack_sha256"
        ],
        "staging_snapshot": snapshot,
        "github_fallback_sha256": file_hashes(
            fallback_root,
            (
                "configpack.zip",
                "configpack.zip.sha256",
                "configpack_version.txt",
            ),
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


def validate_receipt(
    receipt_path: pathlib.Path,
    sidecar_path: pathlib.Path,
    expected_sha: str,
    metadata_path: pathlib.Path,
    staging_snapshot_path: pathlib.Path,
    backend: str,
    staging_name: str,
) -> dict:
    expected_sha = clean_sha(expected_sha, "expected receipt SHA-256")
    if sha256_file(receipt_path) != expected_sha:
        fail("prepared configpack receipt SHA-256 mismatch")
    if parse_sidecar(sidecar_path, receipt_path.name) != expected_sha:
        fail("prepared configpack receipt sidecar mismatch")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    snapshot = json.loads(
        staging_snapshot_path.read_text(encoding="utf-8")
    )
    if (
        receipt.get("schema") != 1
        or receipt.get("kind")
        != "industrial-horizon-configpack-prepared"
        or receipt.get("version") != metadata.get("version")
        or receipt.get("configpack_sha256")
        != metadata.get("configpack_sha256")
        or receipt.get("configpack_size")
        != metadata.get("configpack_size")
        or receipt.get("entry_count") != metadata.get("entry_count")
        or receipt.get("backend") != backend
        or receipt.get("staging_name") != staging_name
        or receipt.get("staging_snapshot") != snapshot
    ):
        fail("prepared configpack receipt does not match this activation")
    if not isinstance(receipt.get("previous_version"), int):
        fail("prepared configpack receipt has an invalid previous version")
    fallback = receipt.get("github_fallback_sha256")
    if not isinstance(fallback, dict) or len(fallback) != 3:
        fail("prepared configpack receipt has invalid fallback hashes")
    for name, digest in fallback.items():
        clean_sha(digest, f"fallback digest for {name}")
    return receipt


def check_fallback(receipt_path: pathlib.Path, fallback_root: pathlib.Path) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected = receipt.get("github_fallback_sha256")
    if not isinstance(expected, dict):
        fail("prepared configpack receipt has no fallback hashes")
    actual = file_hashes(fallback_root, tuple(expected.keys()))
    if actual != expected:
        fail("live configpack GitHub fallback changed after PREPARE")


def make_sftp_batch(
    remote_root: str,
    output: pathlib.Path,
    kind: str,
    staging_name: str,
    rollback_name: str,
) -> None:
    remote_root = validate_remote_root(remote_root)
    staging_root = f"{remote_root}/_staging/{staging_name}"
    rollback_root = f"{remote_root}/_rollback/{rollback_name}"
    if kind == "payload":
        lines = [
            f"-mkdir {sftp_quote(remote_root + '/_staging')}",
            f"-mkdir {sftp_quote(staging_root)}",
        ]
        for name in (
            "configpack.zip",
            "configpack.zip.sha256",
            "configpack_version.txt",
        ):
            lines.append(
                "put "
                + sftp_quote(f"deploy/{name}")
                + " "
                + sftp_quote(f"{staging_root}/{name}")
            )
    elif kind == "activate-payload":
        lines = [
            f"-mkdir {sftp_quote(remote_root + '/_rollback')}",
            f"-mkdir {sftp_quote(rollback_root)}",
        ]
        for name in ("configpack.zip", "configpack.zip.sha256"):
            lines.append(
                "rename "
                + sftp_quote(f"{remote_root}/{name}")
                + " "
                + sftp_quote(f"{rollback_root}/{name}")
            )
        for name in ("configpack.zip", "configpack.zip.sha256"):
            lines.append(
                "rename "
                + sftp_quote(f"{staging_root}/{name}")
                + " "
                + sftp_quote(f"{remote_root}/{name}")
            )
    elif kind == "activate-marker":
        lines = [
            "rename "
            + sftp_quote(f"{remote_root}/configpack_version.txt")
            + " "
            + sftp_quote(f"{rollback_root}/configpack_version.txt"),
            "rename "
            + sftp_quote(f"{staging_root}/configpack_version.txt")
            + " "
            + sftp_quote(f"{remote_root}/configpack_version.txt"),
        ]
    elif kind == "restore":
        lines = []
        for name in (
            "configpack.zip",
            "configpack.zip.sha256",
            "configpack_version.txt",
        ):
            lines.append(
                "put "
                + sftp_quote(f"rollback/cdn/{name}")
                + " "
                + sftp_quote(f"{remote_root}/{name}")
            )
    else:
        fail(f"unsupported configpack SFTP batch kind: {kind}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_public(
    metadata_path: pathlib.Path,
    public_base: str,
    token: str,
    attempts: int,
    interval: int,
    expected_marker: int | None,
) -> None:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_sha = metadata["configpack_sha256"]
    expected_size = int(metadata["configpack_size"])
    last_error: Exception | None = None
    _ = token
    for attempt in range(1, attempts + 1):
        no_cache = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        try:
            sidecar = parse_sidecar_bytes(
                fetch_bytes(
                    f"{public_base.rstrip('/')}/configpack.zip.sha256",
                    headers=no_cache,
                    limit=512,
                ),
                "configpack.zip",
                "public configpack sidecar",
            )
            if sidecar != expected_sha:
                fail("public configpack sidecar mismatch")
            zip_url = f"{public_base.rstrip('/')}/configpack.zip"
            with open_url(
                zip_url, method="HEAD", headers=no_cache
            ) as response:
                try:
                    size = int(response.headers["Content-Length"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValidationError(
                        "public configpack HEAD has no size"
                    ) from exc
            if size != expected_size:
                fail("public configpack HEAD size mismatch")
            remote_size, remote_sha = public_file_hash(
                zip_url, headers=no_cache
            )
            if remote_size != expected_size or remote_sha != expected_sha:
                fail("public configpack full hash mismatch")
            if expected_marker is not None:
                marker = fetch_bytes(
                    f"{public_base.rstrip('/')}/configpack_version.txt",
                    headers=no_cache,
                    limit=128,
                ).decode("utf-8-sig").strip()
                if marker != str(expected_marker):
                    fail(
                        f"public configpack marker is {marker!r}, "
                        f"expected {expected_marker}"
                    )
            return
        except (ValidationError, UnicodeDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                print(
                    f"Configpack CDN is not coherent yet "
                    f"({attempt}/{attempts}); retrying."
                )
                time.sleep(interval)
    raise ValidationError(
        f"public configpack did not become coherent: {last_error}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--deploy", type=pathlib.Path, required=True)
    validate.add_argument("--version", type=int, required=True)
    validate.add_argument("--expected-sha", required=True)

    snapshot = sub.add_parser("snapshot-release")
    snapshot.add_argument("--release-json", type=pathlib.Path, required=True)
    snapshot.add_argument("--expected-tag", required=True)
    snapshot.add_argument("--output", type=pathlib.Path, required=True)

    preflight = sub.add_parser("preflight-active")
    preflight.add_argument("--marker", type=pathlib.Path, required=True)
    preflight.add_argument("--active-root", type=pathlib.Path, required=True)
    preflight.add_argument("--version", type=int, required=True)
    preflight.add_argument("--expected-sha", required=True)
    preflight.add_argument("--output", type=pathlib.Path, required=True)

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

    validate_prepared = sub.add_parser("validate-receipt")
    validate_prepared.add_argument(
        "--receipt", type=pathlib.Path, required=True
    )
    validate_prepared.add_argument(
        "--sidecar", type=pathlib.Path, required=True
    )
    validate_prepared.add_argument("--expected-sha", required=True)
    validate_prepared.add_argument(
        "--metadata", type=pathlib.Path, required=True
    )
    validate_prepared.add_argument(
        "--staging-snapshot", type=pathlib.Path, required=True
    )
    validate_prepared.add_argument("--backend", required=True)
    validate_prepared.add_argument("--staging-name", required=True)

    fallback = sub.add_parser("check-fallback")
    fallback.add_argument("--receipt", type=pathlib.Path, required=True)
    fallback.add_argument(
        "--fallback-root", type=pathlib.Path, required=True
    )

    batch = sub.add_parser("make-sftp-batch")
    batch.add_argument("--remote-root", required=True)
    batch.add_argument("--output", type=pathlib.Path, required=True)
    batch.add_argument(
        "--kind",
        required=True,
        choices=("payload", "activate-payload", "activate-marker", "restore"),
    )
    batch.add_argument("--staging-name", required=True)
    batch.add_argument("--rollback-name", required=True)

    verify = sub.add_parser("verify-public")
    verify.add_argument("--metadata", type=pathlib.Path, required=True)
    verify.add_argument("--public-base", required=True)
    verify.add_argument("--token", required=True)
    verify.add_argument("--attempts", type=int, default=1)
    verify.add_argument("--interval", type=int, default=15)
    verify.add_argument("--expected-marker", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            metadata = validate_configpack(
                args.deploy, args.version, args.expected_sha
            )
            print(
                f"Validated configpack v{metadata['version']}: "
                f"{metadata['entry_count']} files"
            )
        elif args.command == "snapshot-release":
            snapshot_release(
                args.release_json, args.expected_tag, args.output
            )
        elif args.command == "preflight-active":
            preflight_active(
                args.marker,
                args.active_root,
                args.version,
                args.expected_sha,
                args.output,
            )
        elif args.command == "make-receipt":
            digest = make_receipt(
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
            validate_receipt(
                args.receipt,
                args.sidecar,
                args.expected_sha,
                args.metadata,
                args.staging_snapshot,
                args.backend,
                args.staging_name,
            )
        elif args.command == "check-fallback":
            check_fallback(args.receipt, args.fallback_root)
        elif args.command == "make-sftp-batch":
            make_sftp_batch(
                args.remote_root,
                args.output,
                args.kind,
                args.staging_name,
                args.rollback_name,
            )
        elif args.command == "verify-public":
            verify_public(
                args.metadata,
                args.public_base,
                args.token,
                args.attempts,
                args.interval,
                args.expected_marker,
            )
        else:  # pragma: no cover
            fail(f"unsupported command: {args.command}")
    except (
        ValidationError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
