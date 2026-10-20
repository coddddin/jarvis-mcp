def build(context):
    def list_workflows() -> dict:
        """List operator-registered workflow profiles; no processes or browser tabs are opened."""
        return context.workflows.profiles()
    return list_workflows
