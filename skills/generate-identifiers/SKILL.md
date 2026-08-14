---
name: generate-identifiers
version: 3
description: >-
  Use this skill when a user provides a torrent name or file name and wants to fix recognition issues,
  or asks to add/manage custom identifiers (自定义识别词).
  This skill generates identifier rules based on the WordsMatcher preprocessing logic,
  checks for duplicates against existing rules, and saves them via MCP tools.
  Because custom identifiers are global, generated rules must default to conservative,
  sample-specific regex patterns instead of broad matches unless the user explicitly wants global cleanup.
  Applicable scenarios include:
  1) A torrent or file name is incorrectly recognized (wrong title, season, episode, etc.);
  2) The user wants to block unwanted keywords from torrent names;
  3) The user needs episode offset rules for series with non-standard numbering;
  4) The user wants to force recognition of a specific media by source-native ID;
  5) The user wants TV recognition to use a specific TMDB episode group.
allowed-tools: query_custom_identifiers update_custom_identifiers recognize_media
---

# Generate Custom Identifiers (生成自定义识别词)

This skill helps generate custom identifier rules for MoviePilot's media recognition system. Custom identifiers preprocess torrent/file names before the recognition engine runs, correcting naming issues that cause misidentification.

## Prerequisites

You need the following tools:
- `query_custom_identifiers` - Query all existing custom identifier rules
- `update_custom_identifiers` - Save the updated identifier list (replaces the full list)
- `recognize_media` - Test recognition of a torrent title or file path (optional, for verification)

## Supported Rule Formats

There are **four formats**. Operators must have spaces on both sides.

### 1. Block Word (屏蔽词)

Removes matched text from the title. Supports regex.

```
SomeUniqueAlias
```

Use a bare block word only when the token itself is specific enough globally, or when the user explicitly wants a global cleanup rule.

### 2. Replacement (被替换词 => 替换词)

Regex substitution. The left side is a regex pattern, the right side is the replacement (supports backreferences).

```
被替换词 => 替换词
```

**Special replacement for direct ID specification:**
```
被替换词 => {[tmdbid=xxx;type=movie/tv;s=xxx;e=xxx]}
被替换词 => {[doubanid=xxx;type=movie/tv;s=xxx;e=xxx]}
```
Use the source-specific field that matches the target metadata provider:
`tmdbid`, `doubanid`, `bangumiid`, or `anilistid`. Where `s` (season) and `e`
(episode) are optional. For TMDB TV recognition, add `g=xxx` to specify an
episode group:

```
被替换词 => {[tmdbid=xxx;type=tv;g=xxx;s=xxx;e=xxx]}
```

### 3. Episode Offset (集偏移)

Shifts episode numbers found between the front and back delimiter words. `EP` is the placeholder for the original episode number.

```
前定位词 <> 后定位词 >> EP-12
```

### 4. Combined Replacement + Episode Offset

First performs replacement; episode offset only runs if replacement succeeded.

```
被替换词 => 替换词 && 前定位词 <> 后定位词 >> EP-12
```

### Comments

Lines starting with `#` are comments and will be skipped during processing.

## Important Rules for Writing Identifiers

1. **Regex support**: All patterns support regular expressions. Special characters (`. * + ? ^ $ { } [ ] ( ) | \`) must be escaped with `\` when matching literally.
2. **Spaces matter**: The operators ` => `, ` <> `, ` >> `, ` && ` must have spaces on both sides.
3. **One rule per string**: Each element in the identifiers list is one rule.
4. **EP placeholder**: In episode offset expressions, `EP` represents the original episode number. Common patterns:
   - `EP-12` means subtract 12
   - `EP+5` means add 5
   - `EP*2` means multiply by 2
5. **Chinese number support**: Episode offset handles Chinese numbers (一二三四五六七八九十).
6. **Empty replacement**: Using nothing after `=>` is equivalent to a block word.

## Global Scope Guardrails

Custom identifiers are **global**. A new rule affects all future torrent/file recognition, not just the sample provided by the user.

When generating a new rule, default to **the narrowest regex that still fixes the user's sample**:

- Extract the sample's unique anchors first: wrong title alias, year, season/episode marker, group tag, source, resolution, release tag, file extension, or other distinctive fragments.
- The matching side should usually contain **at least two meaningful anchors**, and one of them should normally be the title alias or another highly distinctive identifier from the user-provided sample.
- Prefer matching the **full wrong alias or a stable unique fragment** from the sample, not a short generic substring.
- Avoid generic global rules such as bare `1080p`, `WEB-DL`, `中字`, `国配`, `REPACK`, `S01E01`, or pure numbers unless the user explicitly wants a global cleanup rule.
- If the rule only needs to fix one specific naming pattern, prefer a **contextual replacement** with capture groups/backreferences over a bare block word.
- For episode offset rules, the `前定位词` and `后定位词` should use sample-specific context so the offset only runs on the intended naming pattern.
- For direct media binding, the left side should match the user's specific wrong alias or naming pattern, not a broad season/episode pattern that could hit other media.

### Narrow vs Broad Examples

Bad (too broad for a global rule):
```
REPACK
1080p
S01E01 => {[tmdbid=12345;type=tv;s=1;e=1]}
```

Better (scoped to the user's sample pattern):
```
(\[SubGroup\].*?My\.Show.*?2024.*?)REPACK => \1
Some\.Weird\.Name(?:\.2024)?(?:\.S01E\d+)? => {[tmdbid=12345;type=tv;s=1]}
\[Baha\] <> \[1080P\] >> EP-12
```

Before saving, mentally test the rule against:
- the user's sample: it should match
- unrelated titles with common release tags: it should usually **not** match

## Workflow

### Step 1: Analyze the Problem

Parse the torrent/file name provided by the user. Identify:
- What is being incorrectly recognized (title, season, episode, year, quality, etc.)
- What the correct recognition result should be
- Which identifier format(s) will solve the problem
- Which fragments in the provided sample are unique enough to use as regex anchors, so the rule does not accidentally affect unrelated titles

### Step 2: Generate the Identifier Rule(s)

Write the rule using the appropriate format. Ensure:
- Regex special characters are properly escaped
- Add a comment line (starting with `#`) above the rule to describe what it does
- Test the regex mentally against the provided name to verify correctness
- Because the rule is global, prefer the most specific viable match; if a bare block word would be too broad, rewrite it as a contextual replacement that includes sample-specific anchors

### Step 3: Query Existing Identifiers

Use the `query_custom_identifiers` tool to get all current rules:

```
query_custom_identifiers()
```

### Step 4: Check for Duplicates

Compare each new rule against the existing identifiers:
- **Exact duplicate**: The rule string is identical to an existing rule — skip it
- **Functional duplicate**: A different rule that produces the same effect on the same input (e.g., same regex pattern with trivial whitespace differences) — warn the user
- **Conflict**: An existing rule modifies the same text in a different way — warn the user and ask which to keep

### Step 5: Save the Updated Identifiers

Merge new non-duplicate rules into the existing list, then use `update_custom_identifiers` to save the **complete** list:

```
update_custom_identifiers(
    identifiers=["existing rule 1", "existing rule 2", "# new comment", "new rule"]
)
```

**CRITICAL**: Always include ALL existing rules in the list. This tool replaces the entire list.

### Step 6: Verify (Optional)

If the user wants to verify the rule works, use `recognize_media` to test:

```
recognize_media(title="the torrent title to test")
```

### Step 7: Report

Tell the user:
- What rule(s) were added
- What effect they will have on the title
- Whether any duplicates or conflicts were found

## Common Scenarios and Examples

### Wrong Season/Episode Parsing

**User**: "种子名 `[SubGroup] My Show - 13 [1080P]`，这是第二季第1集，但被识别成第13集"

**Solution**: Episode offset to subtract 12:
```
# My Show 第二季集数偏移（13->1）
\[SubGroup\] <> \[1080P\] >> EP-12
```

### Unwanted Text Causing Wrong Identification

**User**: "种子名 `My.Show.2024.REPACK.1080p.mkv`，REPACK导致识别异常"

**Solution**: Contextual replacement, scoped to this title pattern:
```
# 仅在 My.Show.2024 命名中移除 REPACK
(My\.Show\.2024\.)REPACK(\.1080p) => \1\2
```

### Non-Standard Naming

**User**: "文件名 `[OldName] EP01.mkv`，应该识别为 NewName"

**Solution**: Replacement scoped to the wrong alias:
```
# 将特定错误别名 OldName 替换为 NewName
\[OldName\] => [NewName]
```

### Force TMDB ID Recognition

**User**: "种子名 `Some.Weird.Name.S01E01.1080p.mkv`，识别不到，TMDB ID是12345，是电视剧"

**Solution**: Direct ID specification with a sample-specific alias pattern:
```
# 仅在 Some.Weird.Name 这一命名模式下强制绑定 TMDB ID 12345
Some\.Weird\.Name(?:\.S01E\d+)?(?:\.1080p)? => {[tmdbid=12345;type=tv;s=1]}
```

### Force TMDB Episode Group Recognition

**User**: "种子名 `Some.Weird.Name.S01E01.1080p.mkv`，这是按 TMDB 剧集组 `5ad0ec240e0a26303f00d84d` 排序的电视剧"

**Solution**: Direct TMDB ID specification with `g=...`:
```
# 仅在 Some.Weird.Name 命名模式下绑定 TMDB ID 12345 并指定剧集组
Some\.Weird\.Name(?:\.S01E\d+)?(?:\.1080p)? => {[tmdbid=12345;type=tv;g=5ad0ec240e0a26303f00d84d;s=1]}
```

### Combined Fix

**User**: "种子名 `[Baha][OldTitle][13][1080P]`，标题应该是NewTitle，而且13应该是第二季第1集"

**Solution**: Combined replacement + episode offset:
```
# OldTitle替换为NewTitle并偏移集数
OldTitle => NewTitle && \[Baha\] <> \[1080P\] >> EP-12
```

### Multiple Episode Numbers in One Title

**User**: "种子名 `[Group] Title - 13-14 [1080P]`，应该是第1-2集"

**Solution**: Episode offset (handles multiple numbers between delimiters):
```
# Title 集数偏移
\[Group\] <> \[1080P\] >> EP-12
```

## WordsMatcher Processing Logic Reference

The `WordsMatcher.prepare()` method (in `app/domain/meta/words.py`) processes each rule in order:

1. Skip empty lines and lines starting with `#`
2. Detect format by checking operator presence:
   - Contains ` => ` AND ` && ` AND ` >> ` AND ` <> ` → Combined format (4)
   - Contains ` => ` → Replacement format (2)
   - Contains ` >> ` AND ` <> ` → Episode offset format (3)
   - Otherwise → Block word format (1)
3. For combined format, replacement runs first; episode offset only runs if replacement succeeded
4. Returns the modified title and a list of rules that were actually applied
5. Priority: per-subscribe `custom_words` parameter takes precedence over global `CustomIdentifiers`

## Safety Notes

- Always query existing rules first before updating
- Never remove existing rules unless the user explicitly asks
- Add comment lines before new rules for maintainability
- Remember that new rules are global. If a rule looks broad, rewrite it to include more sample-specific anchors before saving.
- When uncertain about the correct approach, present multiple options and let the user choose
