def build(context):
    def analyze_error_log(error_log: str = '', log_path: str = '', code_path: str = '') -> dict:
        """Analyze supplied log text or project log and optional Python file locally. Redacted hypotheses/diff only; never execute or apply."""
        return context.debug.analyze(error_log, log_path, code_path)
    return analyze_error_log
