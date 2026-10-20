"""One guarded project snapshot creation tool."""


def build(context):
    def create_project_snapshot(project: str) -> dict:
        """Back up projects/<name> inside JarvisWorkspace to bounded ZIP; excludes secrets/caches."""
        return context.snapshots.create(project)
    return create_project_snapshot
