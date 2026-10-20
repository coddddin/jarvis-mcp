def build(context):
    def preview_file_organization(project: str) -> dict:
        """Preview top-level project file classification by extension and UTC modification month. No moves."""
        return context.organizer.preview(project)
    return preview_file_organization
