def build(context):
    def undo_file_organization(preview_id: str) -> dict:
        """Undo a reviewed organization operation with a fresh snapshot and rollback on failure."""
        return context.organizer.undo(preview_id)
    return undo_file_organization
