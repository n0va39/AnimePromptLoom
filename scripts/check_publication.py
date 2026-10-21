"""Read-only publication check. Reports locations, never matched secret values.

Checks unignored working files, the index and locally reachable Git history.
Does not decrypt credentials, contact providers, or publish anything.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PATTERNS = [
    re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(rb"gh[pousr]_[0-9A-Za-z]{20,}"),
    re.compile(rb"github_pat_[0-9A-Za-z_]{30,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def contains_secret(data):
    return any(pattern.search(data) for pattern in PATTERNS)


def safe_location(value):
    data = value.encode("utf-8", errors="replace")
    for pattern in PATTERNS:
        data = pattern.sub(b"[REDACTED]", data)
    return data.decode("utf-8", errors="replace")


def blocked_path(name):
    path = PurePosixPath(name.lower())
    if any(part in {"state", "secrets", ".codex", ".agents", ".venv"} for part in path.parts):
        return True
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return True
    if path.suffix in {".dpapi", ".key", ".pem", ".log", ".gguf", ".safetensors", ".pt", ".pth"}:
        return True
    return (path.suffix == ".csv" and "data" in path.parts) or path.name in {"runtime-validation.json", "character-validation.json"}


def audit(root):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)

    findings = []
    def inspect(data, location):
        if contains_secret(data):
            findings.append({"location":safe_location(location), "reason":"credential pattern"})

    names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")) - {""}
    for name in sorted(names):
        if blocked_path(name):
            findings.append({"location":safe_location(name), "reason":"private file in publication candidates"})
        path = root / name
        if path.is_symlink():
            findings.append({"location":safe_location(name), "reason":"review symbolic link target before publication"})
        elif path.is_file():
            inspect(path.read_bytes(), name)

    for row in git("ls-files", "--stage", "-z").split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, oid, _ = metadata.split()
        if mode == b"160000":  # Submodule commit, not a blob in this repository.
            findings.append({"location":safe_location(name.decode()), "reason":"review submodule separately"})
            continue
        inspect(git("cat-file", "blob", oid.decode()), "index:" + name.decode())

    blobs = 0
    for row in git("rev-list", "--objects", "--all").decode("utf-8").splitlines():
        oid, _, name = row.partition(" ")
        if git("cat-file", "-t", oid).strip() != b"blob":
            continue
        blobs += 1
        location = "history:" + oid[:12]
        if name and blocked_path(name):
            findings.append({"location":location, "reason":"private file in reachable history"})
        inspect(git("cat-file", "blob", oid), location)

    required_ignored = ("state/secrets/gemini.dpapi", "state/sessions.json", ".env", "data/danbooru_tags_classified.csv")
    for name in required_ignored:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", name], capture_output=True).returncode == 0
        if not ignored:
            findings.append({"location":name, "reason":"required ignore rule missing or file already tracked"})
    return {"passed":not findings, "working_files_checked":len(names), "history_blobs_checked":blobs, "findings":findings}


def main():
    try:
        report = audit(Path(__file__).resolve().parents[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        print(json.dumps({"passed":False,"error":"Publication check could not complete. No raw file contents or errors printed."}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
