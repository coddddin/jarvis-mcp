def build(context):
    def stop_workflow(run_id: str) -> dict:
        """Stop services owned by this server and verified by creation time; apps/browser tabs are preserved."""
        return context.workflows.stop(run_id)
    return stop_workflow
