def build(context):
    def preview_workflow(name: str) -> dict:
        """Resolve a registered workflow and issue a one-use 10 minute launch preview."""
        return context.workflows.preview(name)
    return preview_workflow
