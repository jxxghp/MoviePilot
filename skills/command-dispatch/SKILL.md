---
name: command-dispatch
version: 2
description: >-
  Use this skill when the user sends a slash command or asks to run a MoviePilot
  system or plugin command in natural language. Discover the live command
  catalog, resolve the exact command, and dispatch it through the structured API
  gateway.
allowed-tools: moviepilot_api
allowed-api-operations: slash.list slash.run plugin.capabilities
---

# Command Dispatch

Use `moviepilot_api`; retired command tools and MCP aliases do not exist.

1. For a literal `/command ...`, preserve the command text and inspect the live
   catalog with `slash.list` when availability or syntax is uncertain.
2. For natural-language requests, call `slash.list`, then match the returned
   name, description, and category. If a plugin owns the capability, optionally
   call `plugin.capabilities` for its declared commands and actions.
3. Never invent a command or argument. If the live catalog has no match, report
   that no supported command was found.
4. Command execution changes external state. Obtain confirmation unless the user
   already explicitly requested the exact command/action.
5. Dispatch with `slash.run` and `body.command` containing the complete command,
   for example `/sites disable 3`. Check the returned acceptance/result before
   reporting completion.

Examples of intent mapping such as site sync, subscription refresh, cache clear,
or restart are hints only; the live `slash.list` response is authoritative.
