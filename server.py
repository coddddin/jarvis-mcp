"""Stable Codex/Claude entry point; implementation lives in jarvis.app."""
from jarvis.app.server import create_server, load_guardrails, main

if __name__ == "__main__":
    main()
