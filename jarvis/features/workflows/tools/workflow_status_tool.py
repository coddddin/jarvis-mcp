def build(context):
    def get_workflow_status(run_id: str) -> dict:
        """Report current owned process state or explicitly unverified historical run records."""
        return context.workflows.status(run_id)
    return get_workflow_status
