"""One guarded preview-bound merge restore tool."""


def build(context):
    def restore_project_snapshot(preview_id: str) -> dict:
        """Apply a reviewed preview: back up current state, preserve extra files, roll back on failure."""
        return context.snapshots.restore(preview_id)
    return restore_project_snapshot
