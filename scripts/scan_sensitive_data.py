#!/usr/bin/env python3
"""Scan distributable or reader-visible text without echoing matched secrets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Pattern


TEXT_EXTENSIONS = {
    ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".py", ".ps1",
    ".sh", ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx",
}
PLACEHOLDER_MARKERS = {
    "<user>", "<username>", "<token>", "<password>", "<secret>",
    "your_api_key", "your_token", "your_password", "redacted_secret",
    "example.com", "/example/", "\\example\\",
}
PROFILE_OUTPUT_NAMES = {
    "skill": "privacy_result_skill.json",
    "reader-local-private": "privacy_result_reader.json",
    "reader-shareable": "privacy_result_reader.json",
    "reader-output": "privacy_result_reader.json",
    "private-backend": "privacy_result_private_backend.json",
}


def rule_patterns() -> list[tuple[str, Pattern[str], str]]:
    return [
        (
            "PRIVATE_KEY",
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            "secret",
        ),
        (
            "OPENAI_STYLE_KEY",
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            "secret",
        ),
        (
            "GITHUB_TOKEN",
            re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
            "secret",
        ),
        (
            "AWS_ACCESS_KEY",
            re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
            "secret",
        ),
        (
            "BEARER_TOKEN",
            re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
            "secret",
        ),
        (
            "SECRET_ASSIGNMENT",
            re.compile(
                r"\b(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?token|auth[_-]?token)"
                r"\b\s*[:=]\s*[\"']?([^\s\"'#,;]{6,})",
                re.IGNORECASE,
            ),
            "secret",
        ),
        (
            "WINDOWS_USER_PATH",
            re.compile(r"\b[A-Za-z]:\\Users\\([^\\/:*?\"<>|\r\n]+)\\", re.IGNORECASE),
            "private_path",
        ),
        (
            "POSIX_HOME_PATH",
            re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/([^/\s<>]+)/"),
            "private_path",
        ),
        (
            "EMAIL_ADDRESS",
            re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
            "personal_identifier",
        ),
        (
            "PHONE_NUMBER",
            re.compile(
                r"(?<![A-Za-z0-9])"
                r"(?:\+\d{1,3}[- ]?)?"
                r"(?:\d[- ]?){9,14}\d"
                r"(?![A-Za-z0-9])"
            ),
            "personal_identifier",
        ),
    ]


def read_denylist(path: Path | None) -> list[str]:
    if path is None:
        return []
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return values


def is_placeholder(line: str, match_text: str) -> bool:
    # A placeholder elsewhere on the line must not hide a real credential.
    lowered = match_text.lower().strip("\"' ")
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS) or lowered in {
        "user", "username", "sample", "demo"
    }


def is_plausible_phone(match_text: str) -> bool:
    digits = re.sub(r"\D", "", match_text)
    if match_text.startswith("+"):
        return 8 <= len(digits) <= 15
    if "-" not in match_text and " " not in match_text:
        return len(digits) in {10, 11}
    groups = re.findall(r"\d+", match_text)
    return 10 <= len(digits) <= 15 and all(len(group) <= 4 for group in groups)


def severity_for(profile: str, category: str) -> str:
    if profile == "private-backend":
        return "WARNING"
    if category == "secret":
        return "ERROR"
    if profile == "reader-local-private":
        return "WARNING"
    if profile in {"skill", "reader-shareable", "reader-output"}:
        return "ERROR"
    return "WARNING"


def denylist_severity(profile: str) -> str:
    if profile in {"private-backend", "reader-local-private"}:
        return "WARNING"
    return "ERROR"


def iter_text_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in TEXT_EXTENSIONS else []
    return [
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS
    ]


def relative_label(path: Path, root: Path, root_index: int) -> str:
    if root.is_file():
        relative = path.name
    else:
        relative = path.relative_to(root).as_posix()
    return f"root_{root_index}/{relative}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan text for sensitive-data patterns.")
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument(
        "--profile",
        choices=(
            "skill",
            "reader-local-private",
            "reader-shareable",
            "reader-output",
            "private-backend",
        ),
        required=True,
        help="reader-output is a compatibility alias for reader-shareable",
    )
    parser.add_argument("--denylist", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Required result path; its filename is fixed by --profile.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = [path.expanduser().resolve() for path in args.paths]
    for root in roots:
        if not root.exists():
            print("A scan path does not exist.", file=sys.stderr)
            return 2
    denylist_path = args.denylist.expanduser().resolve() if args.denylist else None
    if denylist_path and not denylist_path.is_file():
        print("Denylist is not a file.", file=sys.stderr)
        return 2
    denylist = read_denylist(denylist_path)
    patterns = rule_patterns()
    output_path = args.output.expanduser().resolve()
    expected_output_name = PROFILE_OUTPUT_NAMES[args.profile]
    if output_path.name != expected_output_name:
        print(
            f"Output filename for profile {args.profile} must be {expected_output_name}.",
            file=sys.stderr,
        )
        return 2
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    scanned_files = 0

    for root_index, root in enumerate(roots, start=1):
        for path in sorted(iter_text_files(root), key=lambda item: item.as_posix().lower()):
            if path.resolve() == output_path:
                continue
            if denylist_path is not None and path.resolve() == denylist_path:
                continue
            scanned_files += 1
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                warnings.append(
                    {
                        "code": "UNREADABLE_TEXT",
                        "severity": "WARNING",
                        "location": relative_label(path, root, root_index),
                        "line": None,
                    }
                )
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                for code, pattern, category in patterns:
                    for match in pattern.finditer(line):
                        matched = match.group(0)
                        if code == "PHONE_NUMBER" and not is_plausible_phone(matched):
                            continue
                        if is_placeholder(line, matched):
                            continue
                        finding = {
                            "code": code,
                            "severity": severity_for(args.profile, category),
                            "location": relative_label(path, root, root_index),
                            "line": line_number,
                        }
                        if finding["severity"] == "ERROR":
                            errors.append(finding)
                        else:
                            warnings.append(finding)
                lowered = line.lower()
                for value in denylist:
                    if value.lower() not in lowered:
                        continue
                    finding = {
                        "code": "LOCAL_DENYLIST_MATCH",
                        "severity": denylist_severity(args.profile),
                        "location": relative_label(path, root, root_index),
                        "line": line_number,
                    }
                    if finding["severity"] == "ERROR":
                        errors.append(finding)
                    else:
                        warnings.append(finding)

    canonical_profile = (
        "reader-shareable" if args.profile == "reader-output" else args.profile
    )
    result = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": canonical_profile,
        "status": "FAIL" if errors else "PASS",
        "scanned_files": scanned_files,
        "errors": errors,
        "warnings": warnings,
    }
    atomic_write_json(output_path, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "profile": canonical_profile,
                "scanned_files": scanned_files,
                "errors": len(errors),
                "warnings": len(warnings),
            }
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
