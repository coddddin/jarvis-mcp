def build(context):
    def board_send(agent: str, to: str, body: str, task_id: str = '') -> dict:
        """Send a short message (max 1000 chars) instead of editing shared docs. to is an agent ID (codex:main), an employee ID for any of its sessions (cursor), or * for everyone. Optional task_id links it to a task. No secrets."""
        return context.board.send(agent, to, body, task_id=task_id)
    return board_send
