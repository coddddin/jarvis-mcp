def build(context):
    def board_create(agent: str, title: str, paths: list[str], detail: str = '', depends_on: list[str] | None = None) -> dict:
        """Create an open board task. agent is your self-declared ID such as codex:<session> or claude-code:<session>. paths are project-relative files or folders (trailing /) the work will touch. No secrets."""
        return context.board.create(agent, title, detail, paths, depends_on=depends_on)
    return board_create
