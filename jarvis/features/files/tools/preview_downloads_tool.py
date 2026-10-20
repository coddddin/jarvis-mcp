def build(context):
    def preview_downloads_organization() -> dict:
        """Read-only top-level Downloads metadata simulation. Requires operator opt-in; cannot apply moves."""
        return context.organizer.preview_downloads()
    return preview_downloads_organization
