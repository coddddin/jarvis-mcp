def build(context):
    def list_workflow_runs() -> dict:
        """List persisted workflow run history without adopting or terminating recorded PIDs."""
        return context.workflows.history()
    return list_workflow_runs
