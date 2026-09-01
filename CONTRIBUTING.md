# 🛠 Contributing to IVERT

Thanks for considering a contribution to IVERT! Bug reports, fixes, and
documentation improvements are all welcome. This page covers the conventions
this repository follows so a pull request lands smoothly.

## Development setup

IVERT requires Python 3.12 or newer. For an editable install from a clone:

```bash
git clone https://github.com/continuous-dems/ivert.git
cd ivert
pip install -e .
```

The `fetchez`, `globato`, and `transformez` dependencies are pulled
automatically from the [continuous-dems](https://github.com/continuous-dems)
organization; see the [README](README.md) for the full installation options.

## Code style

Linting and formatting are handled by [Ruff](https://docs.astral.sh/ruff/),
pinned in `.pre-commit-config.yaml` and configured in `pyproject.toml`. Install
the git hooks once, and they run on every commit:

```bash
prek install
```

To check everything ahead of a commit:

```bash
prek run --all-files
```

This repository uses [`prek`](https://prek.j178.dev/) rather than
`pre-commit`. It reads the same `.pre-commit-config.yaml`, so the hook
definitions are unchanged, and pre-commit.ci runs the same hooks on every pull
request. To run Ruff directly:

```bash
ruff check src           # lint
ruff format --check src  # verify formatting; drop --check to reformat
```

Ruff runs with `select = ["ALL"]` and a deliberately shrinking `ignore` list,
tracked in [#40](https://github.com/continuous-dems/ivert/issues/40). Please
don't add a new entry to that list to get a change through — fix the code, or
raise the rule on #40 if it needs discussion.

## Branches and pull requests

`main` is the default branch and is not committed to directly. Work happens on
short-lived feature branches and merges through a pull request. Keep a branch
scoped to one change where you can — it makes both review and the changelog
entry easier to write.

## Changelog

**Every user-visible change gets a `CHANGELOG.md` entry in the same pull request
that makes the change.** Writing it later, at release time, means reconstructing
the reasoning from `git log` after the details have faded.

Add the entry under the `## [Unreleased]` heading at the top of the file,
creating that section if it isn't currently there. Use the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories — `Added`,
`Changed`, `Deprecated`, `Removed`, `Fixed`, `Security` — and reference the pull
request number:

```markdown
## [Unreleased]

### Fixed
- Short statement of what now works (#123). Then the why: what the underlying
  cause was, and anything a user needs to know about the change in behaviour.
```

Two conventions worth following, both visible in the existing entries:

- **Explain the cause, not just the symptom.** IVERT's entries read as short
  narratives, and that is deliberate — they are the best record of why the code
  looks the way it does.
- **Reference the pull request number.** You won't know it until the pull
  request exists, so the usual flow is to open it, then push one further commit
  adding the changelog entry. Reference the issue number instead where one
  applies.

Changes with no user-visible effect — lint configuration, internal refactors,
CI — don't need an entry.

## Releases

Maintainers only. IVERT's version is derived from the git tag by `hatch-vcs`, so
the tag is the source of truth and nothing in `pyproject.toml` needs bumping.
A release starts with a pull request titled "Prepare the X.Y.Z release" that
updates two files by hand:

1. **`CHANGELOG.md`** — rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
   add the `[X.Y.Z]` compare link at the bottom of the file, and repoint the
   `[Unreleased]` link at the new tag.
2. **`CITATION.cff`** — set `version` and `date-released` to match the changelog
   entry exactly. Nothing validates this against the tag, so it drifts silently
   if it is missed.

Once that merges, tagging and publishing a GitHub release triggers
`publish-to-pypi.yml`. The conda-forge package is updated separately, through
the [ivert-feedstock](https://github.com/conda-forge/ivert-feedstock) recipe,
which builds from the PyPI sdist and so has to wait for the PyPI publish.
