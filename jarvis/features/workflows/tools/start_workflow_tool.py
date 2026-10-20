def build(context):
    def start_workflow(preview_id: str) -> dict:
        """Launch a reviewed registered profile; fixed argv, no shell, owned service readiness and cleanup."""
        return context.workflows.start(preview_id)
    return start_workflow
