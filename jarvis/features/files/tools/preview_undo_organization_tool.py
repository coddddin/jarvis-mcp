def build(context):
    def preview_file_organization_undo(operation_id: str) -> dict:
        """Preview moving organized files back; reject changed files and occupied original paths."""
        return context.organizer.preview_undo(operation_id)
    return preview_file_organization_undo
