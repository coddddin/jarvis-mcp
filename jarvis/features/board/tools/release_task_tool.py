def build(context):
    def board_release(agent: str, task_id: str, status: str, summary: str, verification: str = '', commit: str = '') -> dict:
        """Release your claim with a handoff summary. status is open, blocked, review, done or cancelled. done requires verification (commands run and results); commit is an optional git hash."""
        return context.board.release(agent, task_id, status, summary, verification=verification, commit=commit)
    return board_release
