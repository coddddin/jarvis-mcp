def build(context):
    def analyze_program_static(path: str) -> dict:
        """Statically inspect one project JS or Windows PE copy with local REA; never execute the supplied program."""
        return context.integrations.analyze(path)
    return analyze_program_static
