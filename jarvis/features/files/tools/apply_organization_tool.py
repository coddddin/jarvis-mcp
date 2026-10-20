def build(context):
    def apply_file_organization(preview_id: str) -> dict:
        """Apply a reviewed project organization preview with automatic snapshot and no-overwrite moves."""
        return context.organizer.apply(preview_id)
    return apply_file_organization
