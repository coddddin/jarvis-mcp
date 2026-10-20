def build(context):
    def read_public_web_page(url: str) -> dict:
        """Read up to 3000 visible characters from one public HTTPS HTML/text page via pinned IP validation. No redirects, scripts, login, cookies or saved files; errors are not page evidence."""
        return context.web.read_page(url)
    return read_public_web_page
