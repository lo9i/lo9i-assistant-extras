import json

from mcp import Client

from lo9i_youtube import downloads, mcp_server, transcripts


async def _call(tool, /, **args):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(tool, args)
    return tools, result


def _read_only(tool):
    return bool(tool.annotations and tool.annotations.read_only_hint)


async def test_only_reading_a_transcript_is_read_only(monkeypatch):
    monkeypatch.setattr(
        transcripts, "fetch", lambda url, languages, timestamps: transcripts.Text("English", True, "hi")
    )
    tools, result = await _call("get_transcript", url="abc")
    assert {name for name, tool in tools.items() if _read_only(tool)} == {"get_transcript"}
    assert json.loads(result.content[0].text) == {"language": "English", "generated": True, "text": "hi"}


async def test_downloads_give_the_path(monkeypatch, tmp_path):
    def audio(url):
        return downloads.Download("Zoo", str(tmp_path / "Zoo [abc].mp3"), 1.5)

    monkeypatch.setattr(downloads, "audio", audio)
    _, result = await _call("download_audio", url="https://youtu.be/abc")
    assert json.loads(result.content[0].text) == {
        "title": "Zoo",
        "path": str(tmp_path / "Zoo [abc].mp3"),
        "megabytes": 1.5,
    }


async def test_download_transcript_gives_the_path_not_the_text(monkeypatch, tmp_path):
    def save(url, languages, timestamps):
        assert (languages, timestamps) == (["es"], True)
        return transcripts.Text("Spanish", False, "hola"), tmp_path / "Zoo [abc].txt"

    monkeypatch.setattr(transcripts, "save", save)
    _, result = await _call("download_transcript", url="abc", languages=["es"], timestamps=True)
    assert json.loads(result.content[0].text) == {
        "language": "Spanish",
        "generated": False,
        "path": str(tmp_path / "Zoo [abc].txt"),
    }


async def test_errors_reach_the_agent(monkeypatch):
    def video(url, max_height):
        raise downloads.DownloadError("[youtube] abc: Video unavailable")

    monkeypatch.setattr(downloads, "video", video)
    _, result = await _call("download_video", url="abc")
    assert result.is_error and "Video unavailable" in result.content[0].text
