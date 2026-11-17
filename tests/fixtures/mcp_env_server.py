"""A stdio MCP server that reports environment settings, for the fixed_env tests."""
import os

from mcp.server.fastmcp import FastMCP

app = FastMCP('env')


@app.tool(description='Report one environment setting.')
def setting(name: str) -> str:
    return name + '=' + os.environ.get(name, '<unset>')


@app.tool(description='A second tool, never pinned.')
def other(text: str) -> str:
    return text


if __name__ == '__main__':
    app.run()
