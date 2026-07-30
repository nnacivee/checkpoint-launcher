#!/usr/bin/env python3
"""Find the read-only SFTP directory backing the active public mirror."""

from __future__ import annotations

import argparse
import collections
import os
import pathlib
import posixpath
import re
import stat
import sys

import paramiko


REQUIRED_FILES = frozenset(
    {
        "modpack_version.txt",
        "configpack_version.txt",
        "manifest.json",
        "manifest.json.sha256",
        "modpack.zip.sha256",
        "configpack.zip.sha256",
    }
)
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".ssh",
        "_rollback",
        "_staging",
        "boot",
        "cache",
        "dev",
        "etc",
        "lib",
        "lib64",
        "logs",
        "node_modules",
        "proc",
        "run",
        "sbin",
        "sys",
        "tmp",
        "usr",
    }
)
PREFERRED_WORDS = (
    "stable",
    "industrial",
    "horizon",
    "cdn",
    "mirror",
    "download",
    "storage",
    "public",
    "www",
)


class DiscoveryError(RuntimeError):
    """A safe SFTP discovery invariant failed."""


def fail(message: str) -> None:
    raise DiscoveryError(message)


def read_marker(
    sftp: paramiko.SFTPClient, directory: str, name: str
) -> int | None:
    path = posixpath.join(directory, name)
    with sftp.open(path, "rb") as stream:
        payload = stream.read(129)
    if len(payload) > 128:
        return None
    try:
        text = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        return None
    return int(text)


def child_priority(name: str) -> tuple[int, str]:
    lowered = name.casefold()
    preferred = any(word in lowered for word in PREFERRED_WORDS)
    return (0 if preferred else 1, lowered)


def discover(
    sftp: paramiko.SFTPClient,
    preferred_root: str,
    expected_modpack: int,
    expected_configpack: int,
    max_depth: int,
    max_directories: int,
) -> tuple[str, int]:
    seeds: list[str] = []
    for candidate in (preferred_root, sftp.normalize("."), "/"):
        candidate = candidate.rstrip("/") or "/"
        if candidate not in seeds:
            seeds.append(candidate)

    queue: collections.deque[tuple[str, int]] = collections.deque(
        (seed, 0) for seed in seeds
    )
    visited: set[str] = set()
    matches: list[str] = []
    while queue:
        directory, depth = queue.popleft()
        canonical = posixpath.normpath(directory)
        if canonical in visited:
            continue
        visited.add(canonical)
        if len(visited) > max_directories:
            fail(
                "SFTP discovery exceeded the bounded directory limit "
                f"({max_directories})"
            )
        try:
            attributes = sftp.listdir_attr(directory)
        except (OSError, IOError):
            continue

        names = {attribute.filename for attribute in attributes}
        if REQUIRED_FILES.issubset(names):
            modpack = read_marker(
                sftp, directory, "modpack_version.txt"
            )
            configpack = read_marker(
                sftp, directory, "configpack_version.txt"
            )
            if (
                modpack == expected_modpack
                and configpack == expected_configpack
            ):
                matches.append(canonical)

        if depth >= max_depth:
            continue
        children = []
        for attribute in attributes:
            name = attribute.filename
            if (
                name in (".", "..")
                or name.casefold() in SKIP_DIRECTORIES
                or name.startswith(".")
                or not stat.S_ISDIR(attribute.st_mode)
            ):
                continue
            children.append(name)
        for name in sorted(children, key=child_priority):
            queue.append((posixpath.join(directory, name), depth + 1))

    matches = sorted(set(matches), key=lambda value: (len(value), value))
    if not matches:
        fail(
            "No SFTP directory contains the complete active "
            f"modpack/configpack channel ({expected_modpack}/"
            f"{expected_configpack}); scanned {len(visited)} directories"
        )
    if len(matches) != 1:
        fail(
            "SFTP discovery found multiple complete active channel roots: "
            + ", ".join(matches)
        )
    root = matches[0]
    if (
        not root.startswith("/")
        or any(ord(character) < 32 for character in root)
        or '"' in root
        or "\\" in root
    ):
        fail("discovered SFTP root cannot be safely exported")
    return root, len(visited)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preferred-root", required=True)
    parser.add_argument("--expected-modpack", type=int, required=True)
    parser.add_argument("--expected-configpack", type=int, required=True)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--max-directories", type=int, default=2500)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if (
            args.expected_modpack <= 0
            or args.expected_configpack <= 0
            or not 1 <= args.max_depth <= 12
            or not 10 <= args.max_directories <= 10000
        ):
            fail("invalid bounded discovery inputs")
        host = os.environ.get("MIRROR_SFTP_SERVER", "")
        user = os.environ.get("MIRROR_SFTP_USER", "")
        password = os.environ.get("MIRROR_SFTP_PASSWORD", "")
        try:
            port = int(os.environ.get("MIRROR_SFTP_PORT", ""))
        except ValueError as exc:
            raise DiscoveryError("invalid SFTP port") from exc
        if not host or not user or not password or not 1 <= port <= 65535:
            fail("incomplete SFTP connection settings")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        try:
            with client.open_sftp() as sftp:
                root, scanned = discover(
                    sftp,
                    args.preferred_root,
                    args.expected_modpack,
                    args.expected_configpack,
                    args.max_depth,
                    args.max_directories,
                )
        finally:
            client.close()

        args.output.write_text(root + "\n", encoding="utf-8")
        print(
            f"Found one coherent active SFTP root after scanning "
            f"{scanned} directories: {root}"
        )
    except (
        DiscoveryError,
        OSError,
        paramiko.SSHException,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
