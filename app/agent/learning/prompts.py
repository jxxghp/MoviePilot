"""后台复盘提示词：依据本轮证据维护稳定记忆与个人技能。"""

# Copyright (c) 2025 Nous Research. See the accompanying MIT license.
# Source f42f579cf8bac4918ac9599bece71618afadd846/agent/background_review.py.

MEMORY_REVIEW_PROMPT = (
    'Review the conversation above and consider saving to memory if appropriate.\n'
    '\n'
    'Memory has TWO distinct stores — pick the right one for each fact:\n'
    "  • USER.md (memory tool, target='user'): who the user is — persona, preferences, communication and work style, personal details they revealed, and expectations about how you should behave.\n"
    "  • MEMORY.md (memory tool, target='memory'): facts about the ENVIRONMENT you operate in — tool quirks, project conventions, config gotchas, paths and endpoints that matter.\n"
    '\n'
    "One fact goes to ONE store, never both — writing it to both bloats both files until they hit their size limits and crowds out the facts that matter; misrouting it puts it where the next session won't look. If the tool schema lists only one target, that store is the only one enabled — use it and skip the other.\n"
    '\n'
    "If something stands out, save it once, in the right store, using the memory tool with the matching target. If nothing is worth saving, just say 'Nothing to save.' and stop."
)

SKILL_REVIEW_PROMPT = (
    'Review the conversation above and update the skill library. Be ACTIVE — most sessions produce at least one skill update, even if small. A pass that does nothing is a missed learning opportunity, not a neutral outcome.\n'
    '\n'
    'Target shape of the library: CLASS-LEVEL skills, each with a SKILL.md of always-on rules and a small `references/` set of topical depth. Not a flat list of narrow one-session skills, and not an umbrella hoarding a references/ file per session. This shapes HOW you update, not WHETHER you update.\n'
    '\n'
    "What a skill IS: the instructions for doing a class of task the most efficient and correct way, to THIS user's specifications — the procedure, the tools and commands that work, the order, the user's preferences for how the result should look, and the pitfalls that cost time. A future session should be able to follow it and produce what the user wants on the first try. Everything below is about writing that well:\n"
    '  • Procedure first: the steps in the order they are done, with the concrete commands, tool calls, and decision points. Lessons and pitfalls attach to the step they affect.\n'
    "  • A pitfall is a generalizable rule + one clause of WHY (the mechanism), imperative. 'Grep the test tree for the SYMBOL before widening a helper signature — hand-rolled mocks reimplement the old shape and fail on a shard you did not run.' Not a narrative of what happened this session.\n"
    '  • No PR/issue numbers, dates, ticket IDs, or quoted user text as content — the rule must stand without the incident behind it. Keep a short quote ONLY when the quote itself is the clearest statement of the rule.\n'
    '  • The same lesson learned twice is ONE rule. Before adding, search the skill (and its references/) for the rule already stated; strengthen or clarify it rather than appending a second copy.\n'
    "  • Not a duplicate of what the environment already teaches: repo AGENTS.md files, tool schema descriptions, and other always-loaded context. A skill carries the WORKFLOW and the pitfalls; it does not restate the codebase map or a tool's parameter list.\n"
    "  • Always-on rules (standing user preferences, gates that apply to every instance of the task) live in SKILL.md itself, whole. references/ is for depth that is only needed sometimes: a decision table, a recipe, a domain note — each file topical and reusable, never '<date>-<incident>.md'. Prefer extending an existing references/ file over creating one; a skill with dozens of one-off references is the failure shape, not the goal.\n"
    "  • Fix the skill in place when it is wrong: edit the sentence that misled, do not append 'UPDATE: actually...' underneath it.\n"
    '\n'
    'Signals to look for (any one of these warrants action):\n'
    "  • User corrected your style, tone, format, legibility, or verbosity. Frustration signals like 'stop doing X', 'this is too verbose', 'don't format like this', 'why are you explaining', 'just give me the answer', 'you always do Y and I hate it', or an explicit 'remember this' are FIRST-CLASS skill signals, not just memory signals. Update the relevant skill(s) to embed the preference so the next session starts already knowing.\n"
    '  • User corrected your workflow, approach, or sequence of steps. Encode the correction as a pitfall or explicit step in the skill that governs that class of task.\n'
    '  • Non-trivial technique, fix, workaround, debugging path, or tool-usage pattern emerged that a future session would benefit from. Capture it.\n'
    '  • A skill that got loaded or consulted this session turned out to be wrong, missing a step, or outdated. Patch it NOW.\n'
    '\n'
    'Preference order — prefer the earliest action that fits, but do pick one when a signal above fired:\n'
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the conversation for skills the user loaded via /skill-name or you read via skill_view. If any of them covers the territory of the new learning, PATCH that one first (re-load it with skill_view during this review — see Read-before-write below). It is the skill that was in play, so it's the right one to extend — but only if it is curator-managed. Bundled, hub, pinned, and user-owned skills are off-limits to you no matter how relevant (see Protected skills below); for those, fall through to the next option.\n"
    '  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). If no loaded skill fits but an existing class-level skill does, patch it. Add a subsection, a pitfall, or broaden a trigger.\n'
    '  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be packaged with three kinds of support files — use the right directory per kind:\n'
    '     • `references/<topic>.md` — topical depth needed only sometimes: a decision table, a reproduction recipe, provider quirks, condensed domain notes or API excerpts. Name it by TOPIC and extend an existing file when one covers the topic; do not create a per-session or per-incident file, and do not paste error transcripts — distill them to the rule.\n'
    '     • `templates/<name>.<ext>` — starter files meant to be copied and modified (boilerplate configs, scaffolding, a known-good example the agent can `reproduce with modifications`).\n'
    '     • `scripts/<name>.<ext>` — statically re-runnable actions the skill can invoke directly (verification scripts, fixture generators, deterministic probes, anything the agent should run rather than hand-type each time).\n'
    "     Add support files via skill_manage action=write_file with file_path starting 'references/', 'templates/', or 'scripts/'. The umbrella's SKILL.md should gain a one-line pointer to any new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing skill covers the class. The name MUST be at the class level. The name MUST NOT be a specific PR number, error string, feature codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' session artifact. If the proposed name only makes sense for today's task, it's wrong — fall back to (1), (2), or (3).\n"
    '\n'
    "Read-before-write (ENFORCED — skill_manage refuses otherwise): before you patch or edit an existing skill's SKILL.md, call skill_view(name) for that skill during this review. Before you overwrite or remove an EXISTING supporting file, call skill_view(name, file_path=...) for that exact file. Content quoted earlier in the conversation transcript does NOT count — the guard requires a fresh load within this review, and your write must be based on what skill_view just returned. Creating a brand-new skill or adding a NEW supporting file needs no prior read. If a write is refused with a read-before-write error, call skill_view for the named target once and retry the write once; do not loop.\n"
    '\n'
    "User-preference embedding (important): when the user expressed a style/format/workflow preference, the update belongs in the SKILL.md body, not just in memory. Memory captures 'who the user is and what the current situation and state of your operations are'; skills capture 'how to do this class of task for this user'. When they complain about how you handled a task, the skill that governs that task needs to carry the lesson.\n"
    '\n'
    'If you notice two existing skills that overlap, note it in your reply — the background curator handles consolidation at scale.\n'
    '\n'
    'Protected skills (DO NOT edit these):\n'
    "  • Bundled skills (shipped with MoviePilot, e.g. 'moviepilot').\n"
    "  • Hub-installed skills (installed via 'MoviePilot skill installation').\n"
    '  • Skills in skills.external_dirs (externally owned).\n'
    "  • PINNED skills (marked via '/skills pin'). You are an autonomous no-user-present actor, so pin blocks your writes too — content updates included. Only the user, in a foreground session, can change a pinned skill.\n"
    "  • USER-OWNED skills — anything not curator-managed. A skill the user hand-wrote, installed by URL, or asked a foreground agent to create is theirs, not yours; your writes to it WILL be refused. This includes skills that were loaded or consulted this session: being in play does not make one yours to edit. If such a skill is wrong or outdated, say so in your reply and recommend '/skills adopt <name>' — do not try to patch it.\n"
    'If the only skills that need updating are protected, say\n'
    "'Nothing to save.' and stop.\n"
    '\n'
    'Do NOT capture (these become persistent self-imposed constraints that bite you later when the environment changes):\n'
    "  • Environment-dependent failures: missing binaries, fresh-install errors, post-migration path mismatches, 'command not found', unconfigured credentials, uninstalled packages. The user can fix these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not work', 'X tool is broken', 'cannot use Y from execute_code'). These harden into refusals the agent cites against itself for months after the actual problem was fixed.\n"
    '  • Session-specific transient errors that resolved before the conversation ended. If retrying worked, the lesson is the retry pattern, not the original failure.\n'
    "  • One-off task narratives. A user asking 'summarize today's market' or 'analyze this PR' is not a class of work that warrants a skill.\n"
    '\n'
    "  • Unresolved failures: if the session ended WITHOUT actually finding a working method — you tried several things, none worked, and told the user to check manually — do NOT write those attempts up as a 'reliable workflow' or 'recommended approach'. That presents an untested sequence of failures as validated guidance a future session will trust and repeat. Either say 'Nothing to save', or, only if you are independently confident of a real working alternative (not something you are merely guessing might work), capture ONLY that alternative — never the dead ends, and never dressed up as best practice.\n"
    '\n'
    "If a tool failed because of setup state, capture the FIX (install command, config step, env var to set) under an existing setup or troubleshooting skill — never 'this tool does not work' as a standalone constraint.\n"
    '\n'
    "'Nothing to save.' is a real option but should NOT be the default. If the session ran smoothly with no corrections and produced no new technique, just say 'Nothing to save.' and stop. Otherwise, act."
)

COMBINED_REVIEW_PROMPT = (
    'Review the conversation above and update two things:\n'
    '\n'
    '**Memory**: TWO distinct stores — pick the right one for each fact:\n'
    "  • USER.md (memory tool, target='user'): who the user is — persona, preferences, communication and work style, personal details they revealed, and expectations about how you should behave.\n"
    "  • MEMORY.md (memory tool, target='memory'): facts about the ENVIRONMENT you operate in — tool quirks, project conventions, config gotchas, paths and endpoints that matter.\n"
    '\n'
    "One fact goes to ONE store, never both — writing it to both bloats both files until they hit their size limits and crowds out the facts that matter; misrouting it puts it where the next session won't look. If the tool schema lists only one target, that store is the only one enabled — use it and skip the other.\n"
    '\n'
    '**Skills**: how to do this class of task. Be ACTIVE — most sessions produce at least one skill update. A pass that does nothing is a missed learning opportunity, not a neutral outcome.\n'
    '\n'
    'Target shape of the skill library: CLASS-LEVEL skills with a SKILL.md of always-on rules and a small `references/` set of topical depth — not narrow one-session skills, and not an umbrella hoarding a references/ file per session.\n'
    '\n'
    "What a skill IS: the instructions for doing a class of task the most efficient and correct way, to THIS user's specifications — the procedure, the tools and commands that work, the order, the user's preferences for how the result should look, and the pitfalls that cost time. A future session should be able to follow it and produce what the user wants on the first try. Everything below is about writing that well:\n"
    '  • Procedure first: the steps in the order they are done, with the concrete commands, tool calls, and decision points. Lessons and pitfalls attach to the step they affect.\n'
    "  • A pitfall is a generalizable rule + one clause of WHY (the mechanism), imperative. 'Grep the test tree for the SYMBOL before widening a helper signature — hand-rolled mocks reimplement the old shape and fail on a shard you did not run.' Not a narrative of what happened this session.\n"
    '  • No PR/issue numbers, dates, ticket IDs, or quoted user text as content — the rule must stand without the incident behind it. Keep a short quote ONLY when the quote itself is the clearest statement of the rule.\n'
    '  • The same lesson learned twice is ONE rule. Before adding, search the skill (and its references/) for the rule already stated; strengthen or clarify it rather than appending a second copy.\n'
    "  • Not a duplicate of what the environment already teaches: repo AGENTS.md files, tool schema descriptions, and other always-loaded context. A skill carries the WORKFLOW and the pitfalls; it does not restate the codebase map or a tool's parameter list.\n"
    "  • Always-on rules (standing user preferences, gates that apply to every instance of the task) live in SKILL.md itself, whole. references/ is for depth that is only needed sometimes: a decision table, a recipe, a domain note — each file topical and reusable, never '<date>-<incident>.md'. Prefer extending an existing references/ file over creating one; a skill with dozens of one-off references is the failure shape, not the goal.\n"
    "  • Fix the skill in place when it is wrong: edit the sentence that misled, do not append 'UPDATE: actually...' underneath it.\n"
    '\n'
    'Signals that warrant a skill update (any one is enough):\n'
    "  • User corrected your style, tone, format, legibility, verbosity, or approach. Frustration is a FIRST-CLASS skill signal, not just a memory signal. 'stop doing X', 'don't format like this', 'I hate when you Y' — embed the lesson in the skill that governs that task so the next session starts fixed.\n"
    '  • Non-trivial technique, fix, workaround, or debugging path emerged.\n'
    '  • A skill that was loaded or consulted turned out wrong, missing, or outdated — patch it now.\n'
    '\n'
    'Preference order for skills — pick the earliest that fits:\n'
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were loaded via /skill-name or skill_view in the conversation. If one of them covers the learning, PATCH it first (re-load it with skill_view during this review — see Read-before-write below). It was in play; it's the right place — provided it is curator-managed. Protected and user-owned skills are off-limits however relevant; fall through when one of those is the best fit.\n"
    '  2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view to find the right one). Patch it.\n'
    '  3. ADD A SUPPORT FILE under an existing umbrella via skill_manage action=write_file. Three kinds: `references/<topic>.md` for topical depth (decision tables, recipes, quirks, condensed domain notes) — extend an existing topical file before creating one, never a per-session file; `templates/<name>.<ext>` for starter files meant to be copied and modified; `scripts/<name>.<ext>` for statically re-runnable actions (verification, fixture generators, probes). Add a one-line pointer in SKILL.md so future agents find them.\n'
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. Name at the class level — NOT a PR number, error string, codename, library-alone name, or 'fix-X / debug-Y' session artifact. If the name only fits today's task, fall back to (1), (2), or (3).\n"
    '\n'
    "Read-before-write (ENFORCED — skill_manage refuses otherwise): before patching or editing an existing skill's SKILL.md, call skill_view(name) during this review; before overwriting or removing an EXISTING supporting file, call skill_view(name, file_path=...) for that exact file. Content quoted earlier in the transcript does NOT count — base the write on what skill_view just returned. New skills and NEW supporting files need no prior read. On a read-before-write refusal: view the named target once, retry the write once, do not loop.\n"
    '\n'
    "User-preference embedding: when the user complains about how you handled a task, update the skill that governs that task rather than memory. Memory says 'who the user is and what the current situation and state of your operations are'; skills say 'how to do this class of task for this user'. A user-preference lesson lives in exactly ONE place: the skill that governs the task when one exists, USER.md only for cross-cutting preferences no skill owns — never both. Duplicating it is how a memory file ends up restating SKILL.md until both hit their size limits.\n"
    '\n'
    'If you notice overlapping existing skills, mention it — the background curator handles consolidation.\n'
    '\n'
    'Protected skills (DO NOT edit these):\n'
    "  • Bundled skills (shipped with MoviePilot, e.g. 'moviepilot').\n"
    "  • Hub-installed skills (installed via 'MoviePilot skill installation').\n"
    '  • Skills in skills.external_dirs (externally owned).\n'
    "  • PINNED skills (marked via '/skills pin'). Pin blocks autonomous writes entirely — content updates included — because no user is present to consent. Only a foreground session can change one.\n"
    "  • USER-OWNED skills — anything not curator-managed (hand-written, URL-installed, or created by a foreground agent at the user's request). Your writes to these WILL be refused, including to skills loaded or consulted this session. If one is wrong, say so in your reply and recommend '/skills adopt <name>' instead.\n"
    'If the only skills that need updating are protected, say\n'
    "'Nothing to save.' and stop.\n"
    '\n'
    'Do NOT capture as skills (these become persistent self-imposed constraints that bite you later when the environment changes):\n'
    "  • Environment-dependent failures: missing binaries, fresh-install errors, post-migration path mismatches, 'command not found', unconfigured credentials, uninstalled packages. The user can fix these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not work', 'X tool is broken', 'cannot use Y from execute_code'). These harden into refusals the agent cites against itself for months after the actual problem was fixed.\n"
    '  • Session-specific transient errors that resolved before the conversation ended. If retrying worked, the lesson is the retry pattern, not the original failure.\n'
    "  • One-off task narratives. A user asking 'summarize today's market' or 'analyze this PR' is not a class of work that warrants a skill.\n"
    '\n'
    "  • Unresolved failures: if the session ended WITHOUT actually finding a working method — you tried several things, none worked, and told the user to check manually — do NOT write those attempts up as a 'reliable workflow' or 'recommended approach'. That presents an untested sequence of failures as validated guidance a future session will trust and repeat. Either say 'Nothing to save', or, only if you are independently confident of a real working alternative (not something you are merely guessing might work), capture ONLY that alternative — never the dead ends, and never dressed up as best practice.\n"
    '\n'
    "If a tool failed because of setup state, capture the FIX (install command, config step, env var to set) under an existing setup or troubleshooting skill — never 'this tool does not work' as a standalone constraint.\n"
    '\n'
    "Act on whichever of the two dimensions has real signal. If genuinely nothing stands out on either, say 'Nothing to save.' and stop — but don't reach for that conclusion as a default."
)
