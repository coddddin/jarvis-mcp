# Claude Code in J.A.R.V.I.S.

You are an AI employee on this project, listed as `claude-code` in [config/agents.json](config/agents.json).

1. Read [AGENTS_CONTEXT.md](AGENTS_CONTEXT.md) first. It is the shared map and rules for every agent. Do not duplicate its rules here.
2. Coordinate on the shared board with the jarvis MCP tools ([guide](docs/guides/board.md), [protocol](docs/guides/communication.md)). Use agent ID `claude-code:<session>`, for example `claude-code:main`. Start with `board_inbox`. With another live Claude session, prefer Claude Code peer messaging; otherwise `board_send`.
3. `AGENTS.md` and `sources/` are synced from the ChatGPT project: read them, never edit them.
