## TIER IV universe / split mirror (visualization)

This repository split mirrors the visualization subtrees of several upstream repositories, used for TIER IV release workflows.

This branch only holds the mirror configuration and its tooling. See the mirror branches for source code.

### Published branches

| Branch | Contents |
| --- | --- |
| `awf-latest` | `autowarefoundation/autoware_universe:main`, `visualization/` |
| `feat/v0.64/e2e` | `tier4/autoware_universe:feat/v0.64/e2e`, the same paths as the universe mirror |

The mirror branches keep the flat names the previous workflows used, so nothing
that already points at `awf-latest` has to move. Grouping them under an
`awf-latest/` namespace would read better but would require deleting the
existing `awf-latest` branch first, which the organisation rulesets do not
permit here.

### How it works

[`.sync/sources.yaml`](.sync/sources.yaml) is the single source of truth. It
describes every upstream, the paths to retain, the commit-message rewriting and
the branch each mirror is published to. `.github/workflows/mirror.yaml` derives
its job matrices from that file, so adding or changing a mirror is a
configuration change and never a workflow change.

The pipeline has two stages:

1. **Filter.** `tools/mirror.py mirror SOURCE` clones the upstream and runs
   `git-filter-repo` with arguments generated from the configuration, then
   pushes the result to that source's `mirror_branch`.
2. **Combine.** `tools/mirror.py combine TARGET` reads the mirror branches that
   stage 1 published and appends any not-yet-reflected member commits onto the
   already published combined tip (or builds from scratch on first publish).
   It never clones an upstream, so the combined branch cannot disagree with the
   per-source mirrors.

### Determinism and publishing

Per-source mirrors are pure functions of `(upstream commit, .sync/sources.yaml)`:

- `git-filter-repo` rewrites a given history the same way every time. The
  version is pinned in the workflow, because a different version may rewrite
  differently.
- The same upstream tip therefore republishes as a fast-forward. A failed push
  means reproducibility was lost.

The combined branch is the pure function
`f(published tip, member tips)`:

- Member commits not yet reflected in the published tip are appended onto it.
  The resume point is recovered from the tip tree (each member's renamed
  subtree oids). No sidecar ref is required.
- Same published tip plus same member tips always yield the same commit ids, so
  the push is a fast-forward (or unchanged).
- `tools/mirror.py combine --verify` checks that the tip tree matches what the
  current member tips compose to, and that a second build from the same inputs
  reproduces the commit id. The scheduled workflow always passes `--verify`.

### Working on the configuration

```bash
python3 -m pip install pyyaml git-filter-repo==2.47.0

tools/sync_config.py validate                # check the configuration
tools/sync_config.py show autoware_universe  # the git-filter-repo call it implies
```

Without `--push`, `tools/mirror.py mirror` and `tools/mirror.py combine` are dry runs.

### Manual sync

Publishing is **fast-forward only** (never `--force`). This is the local
equivalent of `.github/workflows/mirror.yaml`.

```bash
git checkout ci && git pull origin ci
python3 -m pip install pyyaml 'git-filter-repo==2.47.0'

PUSH=0 ./tools/manual_sync.sh   # dry run (build only)
PUSH=1 ./tools/manual_sync.sh   # push to origin
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUSH` | `1` | Set `0` for a dry run |
| `DOWNSTREAM` | `git@github.com:tier4/pilot_auto_visualization.git` | Git remote to push |
| `WORK` | `/tmp/pilot_auto_visualization-sync` | Scratch directory for clones |

To refresh one source only:

```bash
export PYTHONPATH="$PWD/tools${PYTHONPATH:+:$PYTHONPATH}"
tools/mirror.py mirror SOURCE \
  --work "/tmp/pilot_auto_visualization-sync/mirror" \
  --downstream "git@github.com:tier4/pilot_auto_visualization.git" \
  --push
```

Replace `SOURCE` with a name from `tools/mirror.py list-sources`.
For a combined target, use `tools/mirror.py combine TARGET ... --verify --push`
after its member mirrors are published.
