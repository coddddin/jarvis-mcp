def build(context):
    def collect_web_information(source_ids: list[str], max_items: int = 5, save_report: bool = True) -> dict:
        """Collect up to three registered public sources, return untrusted excerpts/weather, optionally save an exclusive local text report. No search, account login or model calls."""
        return context.web.collect(source_ids, max_items, save_report)
    return collect_web_information
