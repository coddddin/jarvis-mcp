def build(context):
    def board_heartbeat(agent: str, task_id: str, lease_minutes: int = 30) -> dict:
        """Renew your claim lease while still working (5-240 minutes). Only the current owner can renew. Returns only id, status, owner, lease_until and updated."""
        return context.board.brief(context.board.heartbeat(agent, task_id, lease_minutes=lease_minutes))
    return board_heartbeat
