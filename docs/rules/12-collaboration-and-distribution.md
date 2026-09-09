# 12 — Collaboration, Versioning, Build, and Release

## Commit Conventions

This project uses **Conventional Commits**. The release workflow parses commit messages to categorize changelog entries. This is not stylistic — it is functional.

### Format

```
<type>(<optional scope>): <description>

[optional body]

[optional footer]
```

### Commit Types

| Type | When to use |
|---|---|
| `feat` | A new feature visible to users |
| `fix` | A bug fix |
| `docs` | Documentation only changes |
| `chore` | Maintenance, dependency updates, tooling changes |
| `refactor` | Code restructuring without behavior change |
| `test` | Adding or modifying tests |
| `ci` | CI/CD pipeline changes |
| `perf` | Performance improvements |

### Examples

```
feat: support MiniMax audio provider
fix: sign media server image proxy URLs
docs: add MCP client configuration examples
chore: upgrade pydantic to 2.9.0
refactor: extract transfer path resolution into helper
test: add subscribe endpoint validation tests
ci: improve docker build cache
```

### Rules

- Local commits follow the active workflow, an approved plan, or current user authorization. Existing scoped authorization does not require a second confirmation, including when the confirmed project context defines a requested PR as tracking through merge. Push, PR, merge, and release each need coverage by that authorization; a PR request does not by itself authorize a release. Maintainer arrangements and local anchors follow `AGENTS.md`.
- Keep the subject line under 72 characters.
- Use the imperative mood in the subject line ("add", "fix", "remove", not "added", "fixed", "removed").
- If a commit introduces a breaking change, append `!` after the type and include `BREAKING CHANGE:` in the footer.

---

## Branch Policy

- When review or PR intent is already known, create or switch to a focused topic branch before editing. If that intent appears later, preserve valid work while moving it to a suitable branch.
- The main development branch is the project default — check `git branch` rather than assuming it is `main` or `master`.
- Feature work lives on dedicated branches and is merged via pull request.
- Read-only investigation, throwaway diagnosis, and work explicitly kept local do not require a branch solely for process formality.
- Do not force-push to shared branches.

---

## Version Numbers

- Do not casually change version numbers in `version.py` or related files.
- Version changes are part of the release workflow and are only made when the task explicitly involves a release.
- The `FRONTEND_VERSION` field in `version.py` controls which frontend release the CLI and Docker build will download. Only update it as part of a coordinated frontend release.

---

## Docker Build and Release

- The primary Docker image bundles the backend (Python app), frontend static files (from `public/`), and resource data.
- Docker build and release are managed by CI. Do not manually trigger or alter the Docker release flow unless the task explicitly requires it.
- If a Dockerfile change is needed, update `Dockerfile` and verify the build locally before submitting.

---

## CI/CD

- CI runs on every push and pull request. The pipeline typically includes:
  - Dependency installation
  - pytest test suite
  - pylint static analysis
  - Docker image build (on main branch or tags)
- Contributors must report CI failures and obtain a documented maintainer decision before merging with failed checks. A documented maintainer decision may preauthorize the same scoped failures; an existing applicable decision plus merge authorization does not require another approval.
- Record the failed check, evidence of its cause and relationship to the change, and the applicable decision. Maintainers may authorize proceeding with proven unrelated base failures, automation/infrastructure/quota failures, or non-actionable review feedback. Logs, a current target-base reproduction, or other sufficient evidence must show the change did not cause, worsen, or newly expose the issue; failure status alone is insufficient. Do not silently expand the PR to fix unrelated issues or report failed/unrun checks as passed.
- Follow actual required checks, Rulesets, and platform merge restrictions. Unresolved substantive issues owned by the change block merge; a scoped failure decision is not permission to bypass protection or waive shared architecture, compatibility, and correctness contracts. Maintainer authority follows the confirmed user/project context in `AGENTS.md`, not GitHub `WRITE` or a contributor's self-declaration.

---

## Pull Request Guidelines

The following preparation is the contributor default. Confirmed maintainers may adjust verification timing and delivery order under `AGENTS.md`, while keeping the evidence and remaining work explicit.

- Keep PRs focused on a single concern. Separate refactors, features, and bug fixes into distinct PRs when practical.
- Include in the PR description:
  - What changed and why
  - How the change was validated
  - Any known risks or compatibility impact
  - Migration steps if config or database schema changed
- Tag the PR with the appropriate label (`bug`, `feature`, `docs`, `chore`).

---

## Dependency Release Process

When updating a dependency:

1. Decide the dependency layer: runtime packages go to `[project].dependencies`; test, coverage, lint, and explicit build tooling go to `[dependency-groups].dev`.
2. Run `uv lock`, commit the updated `uv.lock`, and verify it with `uv lock --check`.
3. Run `uv sync --locked`, the locked project consistency check, and the runtime dependency audit documented in `03-commands.md`.
4. Run the full test suite: `uv run --locked --no-sync pytest`.

---

## Local CLI Release

The `moviepilot` CLI is the local-mode entrypoint. Its update path is:

```bash
moviepilot update all     # updates backend + frontend + resources
moviepilot update backend # git pull + reinstall deps
moviepilot update frontend
```

Bootstrap installer changes live in `scripts/bootstrap-local.sh`. Only modify this script if the task explicitly involves the bootstrap flow.

*Last Updated: 2026-08-19*
