"""One guarded snapshot inventory tool."""


def build(context):
    def list_project_snapshots() -> dict:
        """List managed snapshots and any unfinished recoveries; no archive payload returned."""
        return context.snapshots.list()
    return list_project_snapshots
