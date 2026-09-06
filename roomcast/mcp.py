"""MCP exposes only the typed Roomcast API, never a shell or arbitrary URL fetch."""
import os

from mcp.server.fastmcp import FastMCP

from .cli import call

app = FastMCP("roomcast")
SOCKET = os.environ.get("ROOMCAST_SOCKET", "/run/roomcast/control.sock")


@app.tool()
async def search(query: str) -> list:
    """Find movie/series IDs. Choose the matching title and year before playback."""
    return await call(SOCKET, "GET", "/search", params={"q": query})


@app.tool()
async def play(kind: str, id: int, season: int = 1, episode: int = 1, replace: bool = False) -> dict:
    """Start an episode/movie. Poll status until playing or failed; queued is not confirmation.

    Set replace only when the user intends to interrupt current viewing.
    """
    return await call(SOCKET, "POST", "/play", data={
        "kind": kind, "id": id, "season": season, "episode": episode, "replace": replace,
    })


@app.tool()
async def status() -> dict:
    """Read the playback job, current Roku player and relay metrics."""
    return await call(SOCKET, "GET", "/status")


@app.tool()
async def control(command: str) -> dict:
    """Control playback: pause, resume, stop, home, volume_up, volume_down, mute, power_on."""
    from .roku import Roku
    if command not in Roku.commands:
        raise ValueError("unsupported command")
    return await call(SOCKET, "POST", "/command/" + command, data={})


def main():
    app.run(transport="stdio")
