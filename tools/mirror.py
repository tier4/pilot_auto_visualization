#!/usr/bin/env python3
"""Publish every mirror described by .sync/sources.yaml.

    mirror.py list-sources                  names of the per-source mirrors
    mirror.py list-combined                 names of the combined branches
    mirror.py mirror SOURCE --work DIR      filter one upstream, optionally push
    mirror.py combine TARGET --work DIR     replay mirror branches into one branch

`mirror` clones the upstream and runs git-filter-repo with arguments derived
from the configuration. `combine` reads the already published mirror branches,
so it never needs an upstream clone and cannot disagree with what was pushed.

Publishing never uses --force. Per-source mirrors are pure functions of
(upstream tip, configuration), so updates are fast-forwards when reproducible.
The combined branch is the pure function f(published tip, member tips): only
not-yet-reflected member commits are appended, with resume recovered from the
tip tree, so the same inputs always yield the same commit ids and the push is
always a fast-forward (or a no-op).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

import combine_history
import sync_config
from combine_history import CombineError, MemberSpec
from sync_config import ConfigError


def _run(args: list[str], cwd: str | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")


def _capture(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _fresh_dir(path: str) -> str:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


# Matched against the remote's rejection message to turn a raw git failure into
# something that names the thing to change.
_PUSH_HINTS = (
    (
        "cannot lock ref",
        "the branch name collides with an existing ref; git cannot hold both "
        "refs/heads/X and refs/heads/X/Y, so the other one has to go first",
    ),
    (
        "Changes must be made through a pull request",
        "a ruleset requires a pull request for this branch and the pushing "
        "identity is not one of its bypass actors",
    ),
    (
        "Cannot update this protected ref",
        "a ruleset protects this branch and the pushing identity is not one of "
        "its bypass actors",
    ),
    (
        "creations being restricted",
        "a ruleset forbids creating branches here and the pushing identity is "
        "not one of its bypass actors",
    ),
    (
        "non-fast-forward",
        "the published history diverged from this build; force pushes are not "
        "used, so the local build must fast-forward the published tip",
    ),
)


def _published_state(repo: str, downstream: str, branch: str, built: str) -> str:
    """Classify what publishing `built` to `branch` would do, and say so.

    A fast-forward means the new tip descends from the published tip. Divergence
    is a hard error: this repository never force-pushes.
    """
    ref = f"refs/mirror-published/{branch}"
    probe = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            "--force",
            downstream,
            f"refs/heads/{branch}:{ref}",
        ],
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        print(f"  {branch}: not published yet, nothing to compare against")
        return "absent"
    published = _capture(["git", "-C", repo, "rev-parse", ref])
    if published == built:
        print(f"  {branch}: unchanged ({built[:12]})")
        return "unchanged"
    ancestor = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", published, built],
        check=False,
    )
    if ancestor.returncode == 0:
        print(f"  {branch}: fast-forward from {published[:12]} to {built[:12]}")
        return "fast-forward"
    print(
        f"  {branch}: ERROR history would diverge, {published[:12]} is not an "
        f"ancestor of {built[:12]}"
    )
    return "diverged"


def _publish(repo: str, downstream: str, local_ref: str, branch: str, built: str) -> None:
    """Push `built` without --force. Divergence fails instead of rewriting."""
    state = _published_state(repo, downstream, branch, built)
    if state == "unchanged":
        print(f"  {branch}: already published, nothing to push")
        return
    if state == "diverged":
        raise RuntimeError(
            f"{branch}: publishing would rewrite published history; "
            "force pushes are not used in this repository"
        )

    result = subprocess.run(
        ["git", "-C", repo, "push", downstream, f"{local_ref}:refs/heads/{branch}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stderr:
        print(result.stderr.rstrip())
    if result.returncode == 0:
        print(f"  pushed {branch}")
        return
    for needle, hint in _PUSH_HINTS:
        if needle in result.stderr:
            raise RuntimeError(f"{branch}: push rejected -- {hint}")
    raise RuntimeError(f"{branch}: push failed ({result.returncode})")


def do_mirror(config: sync_config.Config, args: argparse.Namespace) -> int:
    source = config.sources.get(args.source)
    if source is None:
        raise ConfigError(f"unknown source {args.source!r}")

    work = _fresh_dir(os.path.join(args.work, source.name))
    clone = os.path.join(work, "upstream")
    print(f"cloning {source.upstream} ({source.ref})")
    _run(
        [
            "git",
            "clone",
            "--quiet",
            "--single-branch",
            "--branch",
            source.ref,
            source.upstream,
            clone,
        ]
    )

    filter_args = sync_config.filter_repo_args(source, work)
    print(f"filtering {source.name}: git filter-repo {' '.join(filter_args)}")
    _run(["git", "filter-repo", *filter_args], cwd=clone)

    if subprocess.run(
        ["git", "-C", clone, "rev-parse", "--verify", "--quiet", "HEAD"],
        check=False,
        capture_output=True,
    ).returncode:
        message = f"{source.name}: the configured paths match nothing in {source.ref}"
        if not source.optional:
            raise RuntimeError(message)
        print(f"  {message}; skipping because the source is marked optional")
        return 0

    tip = _capture(["git", "-C", clone, "rev-parse", "HEAD"])
    count = _capture(["git", "-C", clone, "rev-list", "--count", "HEAD"])
    print(f"  {source.name}: {count} commit(s), tip {tip[:12]}")

    if not source.mirror_branch:
        print(f"  {source.name}: no mirror_branch configured, not pushing")
        return 0
    if args.push:
        if not args.downstream:
            raise RuntimeError("--push needs --downstream")
        _publish(clone, args.downstream, "HEAD", source.mirror_branch, tip)
    else:
        if args.downstream:
            _published_state(clone, args.downstream, source.mirror_branch, tip)
        print("  dry run, nothing pushed")
    return 0


def _remote_has_branch(downstream: str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", downstream, f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _member_specs(
    config: sync_config.Config, target: sync_config.Combined, downstream: str
) -> list[MemberSpec]:
    return [
        MemberSpec(
            name=member.source,
            url=downstream,
            ref=f"refs/heads/{config.sources[member.source].mirror_branch}",
            rename=member.rename,
        )
        for member in target.members
    ]


def _fetch_published_tip(repo: str, downstream: str, branch: str) -> str | None:
    """Return the published tip oid, or None when the branch does not exist yet."""
    ref = f"refs/mirror-published/{branch}"
    probe = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            "--force",
            downstream,
            f"refs/heads/{branch}:{ref}",
        ],
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        return None
    return _capture(["git", "-C", repo, "rev-parse", ref])


def _build_combined(
    config: sync_config.Config,
    name: str,
    repo: str,
    downstream: str,
    base: str | None,
) -> str:
    target = config.combined[name]
    members = _member_specs(config, target, downstream)
    _run(["git", "init", "--quiet", repo])
    if base is not None:
        fetched = _fetch_published_tip(repo, downstream, name)
        if fetched != base:
            raise RuntimeError(
                f"{name}: published tip changed during verify "
                f"({base[:12]} -> {fetched[:12] if fetched else 'absent'})"
            )
    return combine_history.combine(repo, name, members, target.order_by, base=base)


def _verify_combined_content(
    config: sync_config.Config, name: str, repo: str, downstream: str, built: str
) -> None:
    """Fail unless the tip tree matches what the current member tips compose to."""
    target = config.combined[name]
    members = _member_specs(config, target, downstream)
    expected = combine_history.expected_tip_tree(repo, members, target.order_by)
    actual = _capture(["git", "-C", repo, "rev-parse", f"{built}^{{tree}}"])
    if actual != expected:
        raise RuntimeError(
            f"content check failed: tip tree is {actual[:12]}, "
            f"member tips compose to {expected[:12]}"
        )
    print(f"  {name}: tip tree {actual[:12]} matches the current member tips")


def do_combine(config: sync_config.Config, args: argparse.Namespace) -> int:
    if args.target not in config.combined:
        raise ConfigError(f"unknown combined target {args.target!r}")
    if not args.downstream:
        raise RuntimeError("combine needs --downstream to read the mirror branches")

    target = config.combined[args.target]
    members, missing = [], []
    for member in target.members:
        source = config.sources[member.source]
        if _remote_has_branch(args.downstream, source.mirror_branch or ""):
            members.append(member)
        elif source.optional:
            # Configured ahead of the upstream change that creates its paths.
            print(f"  {source.mirror_branch}: not published yet, leaving it out")
        else:
            missing.append(source.mirror_branch)
    if missing:
        message = f"{args.target}: member branch(es) not published yet: {', '.join(missing)}"
        if not args.allow_missing_members:
            raise RuntimeError(message)
        print(f"{message}; skipping")
        return 0
    if len(members) < 2:
        print(f"{args.target}: fewer than two members are published yet; skipping")
        return 0
    target.members = members

    work = _fresh_dir(os.path.join(args.work, args.target))
    repo = os.path.join(work, "combined")
    _run(["git", "init", "--quiet", repo])
    base = _fetch_published_tip(repo, args.downstream, args.target)
    if base is None:
        print(f"building {args.target} from scratch (not published yet)")
    else:
        print(f"building {args.target} by appending onto {base[:12]}")

    built = combine_history.combine(
        repo,
        args.target,
        _member_specs(config, target, args.downstream),
        target.order_by,
        base=base,
    )
    print(f"  {args.target}: tip {built[:12]}")

    if args.verify:
        print(f"verifying {args.target} against current member tips")
        _verify_combined_content(config, args.target, repo, args.downstream, built)
        # Same inputs must yield the same commit ids: (published tip or empty, members).
        second = os.path.join(work, "verify")
        label = f"from {base[:12]}" if base is not None else "from scratch"
        print(f"verifying {args.target} is reproducible {label}")
        again = _build_combined(config, args.target, second, args.downstream, base)
        if again != built:
            raise RuntimeError(
                f"determinism check failed: {built} on the first build, {again} on the second"
            )
        print(f"  {args.target}: reproducible, both builds are {built[:12]}")

    if args.push:
        _publish(repo, args.downstream, f"refs/heads/{args.target}", args.target, built)
    else:
        _published_state(repo, args.downstream, args.target, built)
        print("  dry run, nothing pushed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=sync_config.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-sources")
    sub.add_parser("list-combined")

    mirror = sub.add_parser("mirror")
    mirror.add_argument("source")
    mirror.add_argument("--work", required=True)
    mirror.add_argument("--downstream")
    mirror.add_argument("--push", action="store_true")

    combine = sub.add_parser("combine")
    combine.add_argument("target")
    combine.add_argument("--work", required=True)
    combine.add_argument("--downstream")
    combine.add_argument("--push", action="store_true")
    combine.add_argument("--verify", action="store_true")
    combine.add_argument(
        "--allow-missing-members",
        action="store_true",
        help="skip the target instead of failing when a member branch is not published yet",
    )

    args = parser.parse_args()
    config = sync_config.load(args.config)

    if args.command == "list-sources":
        print(json.dumps(sorted(config.sources)))
        return 0
    if args.command == "list-combined":
        print(json.dumps(sorted(config.combined)))
        return 0
    if args.command == "mirror":
        return do_mirror(config, args)
    return do_combine(config, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConfigError, CombineError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
