def build(context):
    def search_public_web(query: str, limit: int = 3) -> dict:
        """Search public web excerpts through fixed Bing HTTPS RSS, max 3 results. No result-page fetching, accounts, cookies, redirects or saved reports. The query is sent to Bing; do not include secrets."""
        return context.web.search(query, limit)
    return search_public_web
