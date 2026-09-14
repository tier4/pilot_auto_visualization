#!/usr/bin/env python3
"""Replay several filtered histories into one linear branch.

Each member history occupies a disjoint set of top-level paths. Every commit is
replayed in its original form -- same author, committer, timestamps and message
-- with only two things rewritten: its parent becomes the previously replayed
commit, and its tree is recomposed from the latest state of every member.

Nothing is synthesised. No merge commit is created and no wall-clock value ever
reaches an object.

The result is the pure function f(published tip, member tips). When a published
tip is supplied, only member commits not yet reflected in that tip are appended;
resume position is recovered from the tip tree itself. The same tip plus the
same members therefore always yield the same commit ids, and publishing is a
fast-forward without --force. With no published tip, the full member histories
are replayed from scratch (first publish).
"""

from __future__ import annotations

import heapq
import subprocess
from dataclasses import dataclass

# git stores directory entries as "40000"; fast-import wants the padded form.
_TREE_MODE = b"40000"
_TREE_MODE_PADDED = b"040000"
# git hash-object -t tree --stdin </dev/null
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class CombineError(Exception):
    """Raised when the member histories cannot be replayed into one branch."""


@dataclass
class MemberSpec:
    name: str
    url: str
    ref: str
    rename: dict[str, str]


@dataclass
class _Commit:
    oid: bytes
    tree: bytes
    author: bytes
    committer: bytes
    message: bytes
    order_key: int


def _git(repo: str, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CombineError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _batch_read(repo: str, oids: list[bytes]) -> dict[bytes, bytes]:
    """Read many objects with a single `git cat-file --batch` invocation."""
    if not oids:
        return {}
    raw = _git(repo, "cat-file", "--batch", stdin=b"\n".join(oids) + b"\n")
    contents: dict[bytes, bytes] = {}
    position = 0
    end = len(raw)
    while position < end:
        newline = raw.index(b"\n", position)
        header = raw[position:newline].split(b" ")
        if len(header) != 3:
            raise CombineError(f"unexpected cat-file header: {raw[position:newline]!r}")
        oid, _, size = header
        start = newline + 1
        length = int(size)
        contents[oid] = raw[start : start + length]
        position = start + length + 1
    return contents


def _parse_commit_object(raw: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Split a raw commit object into (tree, author, committer, message).

    Any signature header is dropped, which matches `fast-export
    --signed-commits=strip`: a signature over the original parent would be void
    after reparenting anyway.
    """
    header, _, message = raw.partition(b"\n\n")
    tree = author = committer = None
    for line in header.split(b"\n"):
        if line.startswith(b" "):
            continue  # continuation of a folded header such as gpgsig
        if line.startswith(b"tree "):
            tree = line[len(b"tree ") :]
        elif line.startswith(b"author "):
            author = line[len(b"author ") :]
        elif line.startswith(b"committer "):
            committer = line[len(b"committer ") :]
    if tree is None or author is None or committer is None:
        raise CombineError("commit object is missing tree, author or committer")
    return tree, author, committer, message


def _parse_tree_object(raw: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """Return the (mode, name, hex oid) entries of a tree object."""
    entries = []
    position = 0
    end = len(raw)
    while position < end:
        space = raw.index(b" ", position)
        mode = raw[position:space]
        nul = raw.index(b"\x00", space)
        name = raw[space + 1 : nul]
        oid = raw[nul + 1 : nul + 21]
        if mode == _TREE_MODE:
            mode = _TREE_MODE_PADDED
        entries.append((mode, name, oid.hex().encode()))
        position = nul + 21
    return entries


def _timestamp(identity: bytes) -> int:
    """Extract the epoch out of an `author`/`committer` header value."""
    try:
        return int(identity.rsplit(b" ", 2)[-2])
    except (IndexError, ValueError) as exc:
        raise CombineError(f"cannot read a timestamp from {identity!r}") from exc


def _quote_path(name: bytes) -> bytes:
    if not name or name.startswith(b'"') or any(byte in name for byte in b'"\n\\'):
        escaped = name.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")
        return b'"' + escaped + b'"'
    return name


def _load_member(repo: str, ref: str, order_by: str) -> list[_Commit]:
    listing = _git(repo, "rev-list", "--topo-order", "--reverse", ref)
    oids = listing.split()
    if not oids:
        raise CombineError(f"{ref} has no commits")
    objects = _batch_read(repo, oids)
    commits = []
    for oid in oids:
        tree, author, committer, message = _parse_commit_object(objects[oid])
        identity = committer if order_by == "committer_date" else author
        commits.append(
            _Commit(
                oid=oid,
                tree=tree,
                author=author,
                committer=committer,
                message=message,
                order_key=_timestamp(identity),
            )
        )
    return commits


def _top_level_state(
    repo: str, commits: list[_Commit], rename: dict[str, str]
) -> list[list[tuple[bytes, bytes, bytes]]]:
    """Pre-render (mode, oid, path) entries for every commit of a member.

    Paths are stored unquoted so the same tuples feed both `git update-index`
    (content checks / resume) and fast-import (which quotes on the way out).
    """
    trees = _batch_read(repo, [commit.tree for commit in commits])
    mapping = {key.encode(): value.encode() for key, value in rename.items()}
    rendered: list[list[tuple[bytes, bytes, bytes]]] = []
    for commit in commits:
        entries: list[tuple[bytes, bytes, bytes]] = []
        taken: set[bytes] = set()
        for mode, name, oid in _parse_tree_object(trees[commit.tree]):
            target = mapping.get(name, name)
            if target in taken:
                # Two entries landing on one name would silently drop whichever
                # was written first, so refuse instead.
                raise CombineError(
                    f"rename maps more than one top-level path onto "
                    f"{target.decode(errors='replace')!r} in {commit.oid.decode()}"
                )
            taken.add(target)
            entries.append((mode, oid, target))
        rendered.append(entries)
    return rendered


def _member_names(entries: list[tuple[bytes, bytes, bytes]]) -> set[bytes]:
    return {path for _, _, path in entries}


def _composed_tree(
    repo: str, current: list[list[tuple[bytes, bytes, bytes]] | None]
) -> str:
    """Build the tree oid that the current member states would publish.

    Uses fast-import so the result matches the trees produced by `combine`.
    """
    entries: list[tuple[bytes, bytes, bytes]] = []
    for group in current:
        if group:
            entries.extend(group)
    ref = b"refs/heads/_combine-compose"
    stream = bytearray()
    stream += b"commit " + ref + b"\n"
    stream += b"committer combine <combine@local> 0 +0000\n"
    stream += b"data 0\n"
    stream += b"deleteall\n"
    for mode, oid, path in entries:
        stream += b"M " + mode + b" " + oid + b" " + _quote_path(path) + b"\n"
    stream += b"done\n"
    _git(repo, "fast-import", "--quiet", "--force", "--done", stdin=bytes(stream))
    tree = _git(repo, "rev-parse", f"{ref.decode()}^{{tree}}").decode().strip()
    _git(repo, "update-ref", "-d", ref.decode())
    return tree


def _fingerprint(
    current: list[list[tuple[bytes, bytes, bytes]] | None],
) -> frozenset[tuple[bytes, bytes]]:
    """Path → oid pairs the current member states would publish."""
    return frozenset(
        (path, oid) for group in current if group for _mode, oid, path in group
    )


def _tree_fingerprint(repo: str, tree: str, paths: set[bytes]) -> frozenset[tuple[bytes, bytes]]:
    """Path → oid pairs present at `tree` for the given paths."""
    found: set[tuple[bytes, bytes]] = set()
    for path in paths:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{tree}:{path.decode()}",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            found.add((path, result.stdout.strip()))
    return frozenset(found)


def _plan_replay(
    histories: list[list[_Commit]],
) -> list[tuple[int, int]]:
    """Return (member_index, position) steps in the stable merge order."""
    queue = [
        (history[0].order_key, index, history[0].oid, 0)
        for index, history in enumerate(histories)
        if history
    ]
    heapq.heapify(queue)
    plan: list[tuple[int, int]] = []
    while queue:
        _, member_index, _, position = heapq.heappop(queue)
        plan.append((member_index, position))
        following = position + 1
        if following < len(histories[member_index]):
            nxt = histories[member_index][following]
            heapq.heappush(queue, (nxt.order_key, member_index, nxt.oid, following))
    return plan


def _entries_match_tree(
    repo: str, tip_tree: str, entries: list[tuple[bytes, bytes, bytes]]
) -> bool:
    """True when every renamed path in `entries` has the same oid on `tip_tree`."""
    for _mode, oid, path in entries:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{tip_tree}:{path.decode()}",
            ],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0 or result.stdout.strip() != oid:
            return False
    return True


def _member_resume_index(
    repo: str,
    tip_tree: str,
    member_states: list[list[tuple[bytes, bytes, bytes]]],
    tip_paths: set[bytes],
) -> int:
    """Return the last member commit index reflected in `tip_tree`, or -1.

    An absent member (no contribution on the tip) returns -1 so its whole
    history is pending. Matching is by renamed subtree oid; when several
    commits share a tree the latest index wins.
    """
    member_paths = {path for entries in member_states for _m, _o, path in entries}
    if not member_paths & tip_paths:
        return -1

    matched = -1
    for index, entries in enumerate(member_states):
        if not entries:
            # Empty tree commit: treat as matching when none of this member's
            # historical paths remain on the tip.
            if not (member_paths & tip_paths):
                matched = index
            continue
        if _entries_match_tree(repo, tip_tree, entries):
            matched = index
    if matched < 0:
        raise CombineError(
            "published tip has paths from a member but none of that member's "
            "commits match the tip subtrees; cannot resume without rewriting"
        )
    return matched


def _resume_after(
    repo: str,
    tip_tree: str,
    plan: list[tuple[int, int]],
    states: list[list[list[tuple[bytes, bytes, bytes]]]],
    n_members: int,
) -> tuple[int, list[int]]:
    """Locate the published tip inside the member histories.

    Returns `(skip, positions)` where `positions[i]` is the last applied index
    for member i (-1 if none), and `skip` is how many plan steps that covers
    when those positions are a prefix of `plan`. The positions are what append
    actually resumes from; `skip` is only used for logging when the tip was
    itself produced by this planner.
    """
    if tip_tree == _EMPTY_TREE:
        return 0, [-1] * n_members

    all_member_paths = {
        path for member_states in states for entries in member_states for _m, _o, path in entries
    }
    tip_fp = _tree_fingerprint(repo, tip_tree, all_member_paths)
    tip_paths = {path for path, _oid in tip_fp}

    positions = [
        _member_resume_index(repo, tip_tree, states[index], tip_paths)
        for index in range(n_members)
    ]

    current: list[list[tuple[bytes, bytes, bytes]] | None] = [
        states[index][pos] if pos >= 0 else None for index, pos in enumerate(positions)
    ]
    if _fingerprint(current) != tip_fp:
        raise CombineError(
            "published tip subtrees do not compose back to the tip tree; "
            "the combined branch cannot be extended without rewriting history"
        )

    # How many plan steps reach exactly these positions (for logging / old tips
    # built by a full replay). Not required for append correctness.
    reached = [-1] * n_members
    skip = 0
    for step, (member_index, position) in enumerate(plan, start=1):
        reached[member_index] = position
        if reached == positions:
            skip = step
            break
    return skip, positions


def combine(
    repo: str,
    branch: str,
    members: list[MemberSpec],
    order_by: str,
    base: str | None = None,
) -> str:
    """Build `branch` inside `repo` by replaying `members`, and return its oid.

    If `base` is the oid of an already published tip, only commits not yet
    reflected in that tip are appended onto it.
    """
    if len(members) < 2:
        raise CombineError("a combined branch needs at least two members")

    histories: list[list[_Commit]] = []
    states: list[list[list[tuple[bytes, bytes, bytes]]]] = []
    occupied: list[set[bytes]] = []
    for index, member in enumerate(members):
        target = f"refs/mirror-members/{member.name}"
        _git(repo, "fetch", "--quiet", "--no-tags", "--force", member.url, f"{member.ref}:{target}")
        commits = _load_member(repo, target, order_by)
        histories.append(commits)
        states.append(_top_level_state(repo, commits, member.rename))
        # Every path the member ever occupies, not just the ones it ends with:
        # a collision anywhere in the history would corrupt the replay.
        occupied.append(set().union(*(_member_names(entry) for entry in states[index])))
        print(f"  {member.name}: {len(commits)} commit(s) from {member.ref}")
        for other in range(index):
            overlap = occupied[other] & occupied[index]
            if overlap:
                names = b", ".join(sorted(overlap)).decode(errors="replace")
                raise CombineError(
                    f"{members[other].name} and {member.name} both provide "
                    f"top-level path(s): {names}"
                )

    plan = _plan_replay(histories)
    total = len(plan)
    positions = [-1] * len(members)
    if base is not None:
        tip_tree = _git(repo, "rev-parse", f"{base}^{{tree}}").decode().strip()
        _skip, positions = _resume_after(repo, tip_tree, plan, states, len(members))
        applied = sum(pos + 1 for pos in positions)
        print(
            f"  resuming from {base[:12]} with {applied}/{total} member commit(s) already applied"
        )
        if all(pos == len(histories[i]) - 1 for i, pos in enumerate(positions)):
            _git(repo, "update-ref", f"refs/heads/{branch}", base)
            return base

    # Append only commits not yet reflected in the tip. Their merge order is the
    # same rule as a full replay, but only over the pending suffix of each member.
    remaining = [
        (member_index, position)
        for member_index, position in plan
        if position > positions[member_index]
    ]
    print(
        f"  replaying {len(remaining)} commit(s) into {branch}"
        + (f" (of {total})" if base is not None else "")
    )

    current: list[list[tuple[bytes, bytes, bytes]] | None] = [
        states[index][pos] if pos >= 0 else None for index, pos in enumerate(positions)
    ]

    stream = bytearray()
    ref = f"refs/heads/{branch}".encode()
    mark = 0
    for member_index, position in remaining:
        commit = histories[member_index][position]
        current[member_index] = states[member_index][position]
        mark += 1

        stream += b"commit " + ref + b"\n"
        stream += b"mark :" + str(mark).encode() + b"\n"
        stream += b"original-oid " + commit.oid + b"\n"
        stream += b"author " + commit.author + b"\n"
        stream += b"committer " + commit.committer + b"\n"
        stream += b"data " + str(len(commit.message)).encode() + b"\n"
        stream += commit.message
        if mark == 1 and base is not None:
            stream += b"from " + base.encode() + b"\n"
        elif mark > 1:
            stream += b"from :" + str(mark - 1).encode() + b"\n"
        stream += b"deleteall\n"
        for entries in current:
            if entries:
                for mode, oid, path in entries:
                    stream += b"M " + mode + b" " + oid + b" " + _quote_path(path) + b"\n"

    stream += b"done\n"
    if base is not None:
        _git(repo, "update-ref", f"refs/heads/{branch}", base)
    _git(repo, "fast-import", "--quiet", "--force", "--done", stdin=bytes(stream))
    return _git(repo, "rev-parse", f"refs/heads/{branch}").decode().strip()


def expected_tip_tree(
    repo: str, members: list[MemberSpec], order_by: str
) -> str:
    """Return the tree oid the combined tip must have for the current members.

    Used by `--verify` to check content without requiring commit-id identity.
    """
    if len(members) < 2:
        raise CombineError("a combined branch needs at least two members")
    current: list[list[tuple[bytes, bytes, bytes]] | None] = []
    for member in members:
        target = f"refs/mirror-members/{member.name}"
        # Caller is expected to have fetched already via combine(); re-fetch is
        # cheap with --force and keeps this usable on its own.
        _git(repo, "fetch", "--quiet", "--no-tags", "--force", member.url, f"{member.ref}:{target}")
        commits = _load_member(repo, target, order_by)
        states = _top_level_state(repo, commits, member.rename)
        current.append(states[-1])
    return _composed_tree(repo, current)
