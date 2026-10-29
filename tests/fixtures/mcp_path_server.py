"""A stdio MCP server whose tools take file paths, for the path-argument guard tests. Tools only echo what they
receive (never touch the file system), so a test sees the exact argument that reached the server."""
from pydantic import BaseModel

from mcp.server.fastmcp import FastMCP

app = FastMCP('paths')


class Options(BaseModel):
    file: str
    level: int = 1


@app.tool(description='Open a binary for static analysis.')
def open_binary(path: str) -> str:
    return 'open:' + path


@app.tool(description='Write a report file.')
def save_report(output_path: str, text: str) -> str:
    return 'saved:' + output_path + '|' + text


@app.tool(description='Look something up by text or URL.')
def look(query: str, url: str = '') -> str:
    return 'look:' + query + '|' + url


@app.tool(description='Compare several files.')
def compare(files: list[str]) -> str:
    return 'compare:' + ';'.join(files)


@app.tool(description='Configure with nested options.')
def configure(options: Options) -> str:
    return 'configure:' + options.file


if __name__ == '__main__':
    app.run()
