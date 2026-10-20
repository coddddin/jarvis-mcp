def build(context):
    def list_file_organization_operations() -> dict:
        """List persistent organization history and unfinished recoveries."""
        return context.organizer.operations()
    return list_file_organization_operations
