#!/usr/bin/env python3
"""Load .sync/sources.yaml and derive everything the mirror pipeline needs.

The configuration is the single source of truth: the git-filter-repo arguments,
the commit-message rewriting and the combined-branch layout are all generated
here instead of being written out again in a workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_CONFIG = ".sync/sources.yaml"


class ConfigError(Exception):
    """Raised when sources.yaml does not describe a usable pipeline."""


@dataclass
class MessageRule:
    replacement: str
    pattern: str | None = None
    literal: str | None = None
    since: int | None = None
    until: int | None = None

    @property
    def conditional(self) -> bool:
        return self.since is not None or self.until is not None


@dataclass
class Source:
    name: str
    upstream: str
    ref: str
    paths: list[dict[str, str]]
    message_rules: list[MessageRule] = field(default_factory=list)
    mirror_branch: str | None = None
    force: bool = False
    # An upstream path that does not exist yet. The mirror is skipped instead of
    # failing, and a combined target leaves the member out, so the mapping can
    # be configured ahead of the upstream change that creates the path.
    optional: bool = False


@dataclass
class Member:
    source: str
    rename: dict[str, str] = field(default_factory=dict)


@dataclass
class Combined:
    name: str
    members: list[Member]
    order_by: str = "committer_date"
    force: bool = False


@dataclass
class Config:
    sources: dict[str, Source]
    combined: dict[str, Combined]


def _to_epoch(value: Any, where: str) -> int:
    """Accept an ISO-8601 string or a YAML timestamp and return a UTC epoch."""
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime.combine(value, dt.time.min)
    elif isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(f"{where}: not an ISO-8601 timestamp: {value!r}") from exc
    else:
        raise ConfigError(f"{where}: not a timestamp: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def _parse_message_rule(raw: dict[str, Any], where: str) -> MessageRule:
    if "replacement" not in raw:
        raise ConfigError(f"{where}: message rule needs a 'replacement'")
    has_pattern = "pattern" in raw
    has_literal = "literal" in raw
    if has_pattern == has_literal:
        raise ConfigError(f"{where}: message rule needs exactly one of 'pattern' or 'literal'")
    return MessageRule(
        replacement=str(raw["replacement"]),
        pattern=str(raw["pattern"]) if has_pattern else None,
        literal=str(raw["literal"]) if has_literal else None,
        since=_to_epoch(raw["since"], f"{where}.since") if "since" in raw else None,
        until=_to_epoch(raw["until"], f"{where}.until") if "until" in raw else None,
    )


def _parse_source(name: str, raw: dict[str, Any]) -> Source:
    where = f"sources.{name}"
    for key in ("upstream", "ref", "paths"):
        if key not in raw:
            raise ConfigError(f"{where}: missing required key '{key}'")
    paths = []
    for index, entry in enumerate(raw["paths"]):
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}.paths[{index}]: expected a mapping")
        if ("path" in entry) == ("regex" in entry):
            raise ConfigError(f"{where}.paths[{index}]: needs exactly one of 'path' or 'regex'")
        if "regex" in entry and "rename" in entry:
            raise ConfigError(f"{where}.paths[{index}]: 'rename' is not supported with 'regex'")
        paths.append({str(k): str(v) for k, v in entry.items()})
    rules = [
        _parse_message_rule(entry, f"{where}.message_rules[{index}]")
        for index, entry in enumerate(raw.get("message_rules", []))
    ]
    source = Source(
        name=name,
        upstream=str(raw["upstream"]),
        ref=str(raw["ref"]),
        paths=paths,
        message_rules=rules,
        mirror_branch=raw.get("mirror_branch"),
        force=bool(raw.get("force", False)),
        optional=bool(raw.get("optional", False)),
    )
    if source.force:
        raise ConfigError(
            f"{where}: force pushes are not supported; omit 'force' or set it to false"
        )
    return source


def _parse_combined(name: str, raw: dict[str, Any], sources: dict[str, Source]) -> Combined:
    where = f"combined.{name}"
    if "members" not in raw:
        raise ConfigError(f"{where}: missing required key 'members'")
    members = []
    for index, entry in enumerate(raw["members"]):
        spot = f"{where}.members[{index}]"
        if not isinstance(entry, dict) or "source" not in entry:
            raise ConfigError(f"{spot}: expected a mapping with a 'source'")
        source_name = str(entry["source"])
        source = sources.get(source_name)
        if source is None:
            raise ConfigError(f"{spot}: unknown source {source_name!r}")
        if not source.mirror_branch:
            raise ConfigError(
                f"{spot}: source {source_name!r} has no 'mirror_branch'; a combined "
                "target is assembled from the published mirror branches"
            )
        rename = {str(k): str(v) for k, v in (entry.get("rename") or {}).items()}
        for old, new in rename.items():
            # The key names a top-level entry of the member, so it cannot be
            # nested. The value may be, which is how a member that occupies
            # several top-level directories is filed under one of them.
            if "/" in old.strip("/"):
                raise ConfigError(f"{spot}.rename: only a top-level name can be renamed: {old!r}")
            if not new.strip("/"):
                raise ConfigError(f"{spot}.rename: {old!r} has an empty destination")
        members.append(Member(source=source_name, rename=rename))
    if len(members) < 2:
        raise ConfigError(f"{where}: needs at least two members")
    order_by = str(raw.get("order_by", "committer_date"))
    if order_by not in ("committer_date", "author_date"):
        raise ConfigError(f"{where}.order_by: must be committer_date or author_date")
    combined = Combined(
        name=name,
        members=members,
        order_by=order_by,
        force=bool(raw.get("force", False)),
    )
    if combined.force:
        raise ConfigError(
            f"{where}: force pushes are not supported; omit 'force' or set it to false"
        )
    return combined


def _check_ref_hierarchy(branches: dict[str, str]) -> None:
    """Reject branch names that cannot coexist as git refs.

    refs/heads/a is a file and refs/heads/a/b needs refs/heads/a to be a
    directory, so git refuses to create the second while the first exists.
    Catching it here turns a mid-run push failure into a configuration error.
    """
    for name, owner in sorted(branches.items()):
        for other, other_owner in sorted(branches.items()):
            if other.startswith(name + "/"):
                raise ConfigError(
                    f"{owner}: branch {name!r} cannot coexist with {other!r} "
                    f"from {other_owner}; a ref cannot be both a branch and a "
                    "namespace holding other branches"
                )


def load(path: str = DEFAULT_CONFIG) -> Config:
    with open(path, "rb") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    if raw.get("version") != 1:
        raise ConfigError(f"{path}: unsupported version {raw.get('version')!r}")
    sources = {name: _parse_source(name, body) for name, body in (raw.get("sources") or {}).items()}
    if not sources:
        raise ConfigError(f"{path}: no sources defined")
    branches: dict[str, str] = {}
    for source in sources.values():
        if not source.mirror_branch:
            continue
        clash = branches.get(source.mirror_branch)
        if clash:
            raise ConfigError(
                f"sources.{source.name}: mirror_branch {source.mirror_branch!r} "
                f"is already used by {clash!r}"
            )
        branches[source.mirror_branch] = source.name
    combined = {
        name: _parse_combined(name, body, sources)
        for name, body in (raw.get("combined") or {}).items()
    }
    for name in combined:
        if name in branches:
            raise ConfigError(f"combined.{name}: collides with a mirror_branch")
        branches[name] = f"combined.{name}"
    _check_ref_hierarchy(branches)
    return Config(sources=sources, combined=combined)


def _python_bytes_literal(text: str) -> str:
    """Render text as a Python bytes literal usable inside a generated callback."""
    return repr(text.encode())


def _replace_message_file(source: Source) -> str:
    """Render the --replace-message file for a source with unconditional rules."""
    lines = []
    for rule in source.message_rules:
        if rule.pattern is not None:
            lines.append(f"regex:{rule.pattern}==>{rule.replacement}")
        else:
            lines.append(f"{rule.literal}==>{rule.replacement}")
    return "\n".join(lines) + "\n"


def _commit_callback(source: Source) -> str:
    """Render a --commit-callback body implementing date-conditional rules.

    Emitted only when at least one rule is conditional, so sources with plain
    rules keep using --replace-message and their published history is left
    byte-for-byte identical to what the previous workflows produced.
    """
    lines = ["_date = int(commit.author_date.split()[0])"]
    for rule in source.message_rules:
        guards = []
        if rule.since is not None:
            guards.append(f"_date >= {rule.since}")
        if rule.until is not None:
            guards.append(f"_date < {rule.until}")
        if rule.pattern is not None:
            action = (
                f"commit.message = re.sub({_python_bytes_literal(rule.pattern)}, "
                f"{_python_bytes_literal(rule.replacement)}, commit.message)"
            )
        else:
            action = (
                f"commit.message = commit.message.replace("
                f"{_python_bytes_literal(rule.literal or '')}, "
                f"{_python_bytes_literal(rule.replacement)})"
            )
        if guards:
            lines.append(f"if {' and '.join(guards)}:")
            lines.append(f"    {action}")
        else:
            lines.append(action)
    return "\n".join(lines) + "\n"


def filter_repo_args(source: Source, workdir: str) -> list[str]:
    """Build the git-filter-repo argument list, writing helper files into workdir."""
    args: list[str] = []
    for entry in source.paths:
        if "regex" in entry:
            args += ["--path-regex", entry["regex"]]
        else:
            args += ["--path", entry["path"]]
            if "rename" in entry:
                args += ["--path-rename", f"{entry['path']}:{entry['rename']}"]
    if source.message_rules:
        if any(rule.conditional for rule in source.message_rules):
            args += ["--commit-callback", _commit_callback(source)]
        else:
            target = os.path.join(workdir, f"{source.name}-messages.txt")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(_replace_message_file(source))
            args += ["--replace-message", target]
    args.append("--force")
    return args


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("list-sources")
    sub.add_parser("list-combined")
    show = sub.add_parser("show")
    show.add_argument("source")
    args = parser.parse_args()

    config = load(args.config)
    if args.command == "validate":
        print(
            f"ok: {len(config.sources)} source(s), {len(config.combined)} combined target(s)",
        )
    elif args.command == "list-sources":
        print(json.dumps(sorted(config.sources)))
    elif args.command == "list-combined":
        print(json.dumps(sorted(config.combined)))
    elif args.command == "show":
        source = config.sources.get(args.source)
        if source is None:
            raise ConfigError(f"unknown source {args.source!r}")
        with tempfile.TemporaryDirectory() as tmp:
            rendered = filter_repo_args(source, tmp)
            for index, item in enumerate(rendered):
                if index and rendered[index - 1] == "--replace-message":
                    # Show the generated file rather than its temporary path.
                    with open(item, encoding="utf-8") as handle:
                        print(f"<<\n{handle.read()}>>")
                    continue
                print(item)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except ConfigError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
