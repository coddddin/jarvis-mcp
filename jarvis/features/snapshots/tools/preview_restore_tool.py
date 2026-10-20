"""One guarded restore preview tool."""


def build(context):
    def preview_project_restore(snapshot_id: str) -> dict:
        """Preview merge restore to the original project. Returns a one-use 10 minute preview ID."""
        return context.snapshots.preview(snapshot_id)
    return preview_project_restore
