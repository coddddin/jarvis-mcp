def build(context):
    def board_claim(agent: str, task_id: str, lease_minutes: int = 30) -> dict:
        """Claim a task before editing its paths. Refused if another agent holds an active claim on overlapping paths or dependencies are unfinished. Expired claims can be taken over and are noted. Advisory, not an OS lock."""
        return context.board.claim(agent, task_id, lease_minutes=lease_minutes)
    return board_claim
