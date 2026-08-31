---
name: create-moviepilot-skill
version: 3
description: >-
  Use this skill when the user asks to create, scaffold, update, or review a
  MoviePilot agent skill. This includes adding a new built-in skill under the
  repository `skills/` directory, editing an existing built-in skill, writing
  `SKILL.md` frontmatter and workflow instructions, choosing `allowed-tools`,
  adding helper scripts when needed, and bumping the built-in skill `version`
  so changes can sync into `config/agent/skills`.
allowed-tools: read_file write_file edit_file apply_patch execute_command
---

# Create MoviePilot Skill

This skill guides you through creating or updating a built-in MoviePilot agent
skill in this repository.

## Scope

Use this workflow for repository built-in skills:

- Create or update files under `skills/<skill-id>/`
- Commit the skill as part of the MoviePilot repository
- Do not place the implementation only in `config/agent/skills` unless the user
  explicitly asks for a local override instead of a built-in skill

## MoviePilot-Specific Rules

- The repository root `skills/` directory is the bundled source of truth for
  built-in skills.
- On agent startup, bundled skills are synced into `config/agent/skills`.
- Sync overwrite depends on the `version` field in `SKILL.md`. If you update an
  existing built-in skill, increment `version`, or users may continue using an
  older copied version.
- Keep the folder name and frontmatter `name` identical. Use lowercase letters,
  digits, and hyphens only.
- Prefer extending an existing skill instead of creating an overlapping
  duplicate.

## Workflow

### Step 1: Understand the Request

- Determine whether the user wants a new skill or a change to an existing one.
- Extract the target task, likely trigger phrases, needed tools, and whether
  helper scripts are necessary.
- If the goal is still ambiguous after reading the request and local context,
  ask one focused clarification question. Otherwise proceed with a reasonable
  default.

### Step 2: Check Existing Skills First

- Inspect the repository `skills/` directory before creating anything new.
- If an existing skill already covers most of the workflow, update it instead of
  adding a near-duplicate.
- Reuse the repository style: concise YAML frontmatter, trigger-rich
  description, and procedural body sections.

### Step 3: Choose the Skill ID and Path

- New built-in skill path: `skills/<skill-id>/SKILL.md`
- Keep `<skill-id>` short, hyphen-case, and under 64 characters.
- Use a verb-led or domain-led name that makes the trigger obvious, such as
  `transfer-failed-retry`, `moviepilot-api`, or `create-moviepilot-skill`.

### Step 4: Write Frontmatter Correctly

Use this shape:

```markdown
---
name: create-moviepilot-skill
version: 1
description: >-
  Explain what the skill does and exactly when to use it.
allowed-tools: read_file write_file edit_file execute_command
---
```

Rules:

- `description` is the primary trigger surface. Put concrete "when to use"
  scenarios there.
- Include `version` for built-in skills. Increment it whenever you ship a new
  built-in revision.
- Add `allowed-tools` when the workflow depends on a small, well-defined tool
  set.
- Add `compatibility` only when environment constraints actually matter.

### Step 5: Write the Body

The body should contain:

- A short purpose statement
- MoviePilot-specific rules or guardrails
- A step-by-step workflow
- Concrete examples of matching user requests
- References to supporting files when they exist

Prefer:

- Imperative instructions
- Concrete file paths
- Examples aligned with actual MoviePilot conventions

Avoid:

- Generic theory that does not change execution
- Large duplicated documentation
- Extra files like `README.md` or `CHANGELOG.md` inside the skill directory

### Step 6: Add Supporting Files Only When They Help

- Add `scripts/` only when the same deterministic work would otherwise be
  rewritten repeatedly.
- Keep helper files inside the same skill directory.
- Reference helper paths explicitly from `SKILL.md`.
- If the skill is instructions-only, keep it to a single `SKILL.md`.

### Step 7: Implement the Skill

For a new built-in skill:

1. Create `skills/<skill-id>/`
2. Create `SKILL.md`
3. Add helper scripts only if they are justified

For an existing built-in skill:

1. Edit `skills/<skill-id>/SKILL.md`
2. Increment `version`
3. Update helper files in the same directory if needed

### Step 8: Validate Before Finishing

- Re-read the frontmatter and confirm `name` matches the directory name.
- Confirm `description` mentions real trigger scenarios.
- If you changed an existing built-in skill, confirm `version` increased.
- If possible, validate the file can be parsed by the MoviePilot skills loader.
- Report the final path and note whether the agent needs a restart to sync the
  latest built-in skill into `config/agent/skills`.

## Minimal Example

User request:

`给 MoviePilot agent 加一个处理站点 Cookie 更新的内置技能`

Expected outcome:

- Create or update a directory such as `skills/update-site-cookie/`
- Write `SKILL.md` with a trigger-rich `description`
- Include only the tools needed for that workflow
- Increment `version` when revising an existing built-in skill

## Final Checklist

- Is the skill under the repository `skills/` directory?
- Does the folder name equal frontmatter `name`?
- Does `description` clearly say when the skill should trigger?
- Did you avoid duplicating an existing skill unnecessarily?
- Did you increment `version` for built-in skill updates?
- Did you keep the skill lean and procedural?
