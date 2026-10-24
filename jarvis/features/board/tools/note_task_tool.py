def build(context):
    def board_note(agent: str, task_id: str, kind: str, body: str, refs: list[str] | None = None) -> dict:
        """Add a note to a task. kind is handoff, finding or question. refs can name files, reports or commits. Returns only id, status, owner, lease_until and updated; use board_get for the rest. No secrets."""
        return context.board.brief(context.board.note(agent, task_id, kind, body, refs))
    return board_note
