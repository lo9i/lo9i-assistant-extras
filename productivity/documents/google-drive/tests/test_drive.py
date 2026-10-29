"""Drive, Docs and Sheets: the calls each tool makes and what it answers, against a fake Google."""

from urllib.parse import unquote

import httpx
import pytest

from lo9i_google_drive import drive
from lo9i_google_drive.google import GoogleError
from tests.conftest import body

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUv"
SHEET_ID = "1SheetIdForTheBudget0000"
FOLDER_ID = "0FolderIdForWork00000"
DOC = {"id": DOC_ID, "name": "Plan", "mimeType": drive.DOCUMENT, "modifiedTime": "2026-10-28T10:00:00Z",
       "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit",
       "owners": [{"displayName": "Me", "emailAddress": "me@gmail.com"}]}  # fmt: skip


def test_a_file_is_named_by_its_id_or_link():
    assert drive.file_id(DOC_ID) == DOC_ID and drive.file_id("root") == "root"
    assert drive.file_id(f"https://docs.google.com/document/d/{DOC_ID}/edit?tab=t.0") == DOC_ID
    assert drive.file_id(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0") == SHEET_ID
    assert drive.file_id(f"https://drive.google.com/drive/folders/{FOLDER_ID}") == FOLDER_ID
    assert drive.file_id(f"https://drive.google.com/open?id={DOC_ID}") == DOC_ID
    with pytest.raises(GoogleError, match="isn't a file's id or link: find the file with search_files"):
        drive.file_id("the budget")


async def test_search_looks_in_names_and_text_of_files_not_trashed(fake):
    fake.on("GET", "/drive/v3/files", {"files": [DOC], "nextPageToken": "next"})
    found = await drive.search_files(
        "Ana's plan", kind="document", folder=f"https://drive.google.com/drive/folders/{FOLDER_ID}"
    )
    params = fake.last("GET", "/drive/v3/files").url.params
    assert params["q"] == (
        "trashed = false and (name contains 'Ana\\'s plan' or fullText contains 'Ana\\'s plan') "
        f"and mimeType = '{drive.DOCUMENT}' and '{FOLDER_ID}' in parents"
    )
    assert "orderBy" not in params and params["includeItemsFromAllDrives"] == "true"
    assert found == {
        "files": [
            {
                "id": DOC_ID,
                "name": "Plan",
                "kind": "Google Doc",
                "modified": "2026-10-28T10:00:00Z",
                "owner": "me@gmail.com",
                "link": DOC["webViewLink"],
            }
        ],
        "more": "There are more: narrow the search (a kind, a folder, a date) or ask for a higher limit.",
    }


async def test_without_words_the_latest_changed_come_first(fake):
    fake.on("GET", "/drive/v3/files", {"files": []})
    assert await drive.search_files(modified_after="2026-10-01", limit=500) == {"files": []}
    params = fake.last("GET", "/drive/v3/files").url.params
    assert params["q"] == "trashed = false and modifiedTime > '2026-10-01'"
    assert (params["orderBy"], params["pageSize"]) == ("modifiedTime desc", "50")
    with pytest.raises(GoogleError, match="kind must be one of document, spreadsheet"):
        await drive.search_files(kind="video")


async def test_a_doc_is_read_as_markdown_in_parts(fake, monkeypatch):
    monkeypatch.setattr(drive, "MAX_CHARS", 10)
    fake.on("GET", f"/drive/v3/files/{DOC_ID}", DOC)
    fake.on("GET", f"/drive/v3/files/{DOC_ID}/export", "# Plan\n\n- one\n- two\n")
    read = await drive.read_file(DOC["webViewLink"])
    assert fake.last("GET", f"/drive/v3/files/{DOC_ID}/export").url.params["mimeType"] == "text/markdown"
    assert (read["name"], read["kind"], read["content"], read["characters"]) == (
        "Plan",
        "Google Doc",
        "# Plan\n\n- ",
        20,
    )
    assert read["more"] == "Cut at character 10 of 20: read on with start=10."
    rest = await drive.read_file(DOC_ID, start=19)
    assert rest["content"] == "\n" and "more" not in rest


def _pdf(text: str) -> bytes:
    """A one-page PDF that says `text`."""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(content), content),
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj%s endobj\n" % (n, obj)
    xref = b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    xref += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return pdf + xref + b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, len(pdf))


async def test_pdfs_and_text_files_are_downloaded_and_read(fake):
    pdf = {"id": "1PdfFileId000", "name": "Invoice.pdf", "mimeType": "application/pdf", "size": "900"}
    fake.on("GET", "/drive/v3/files/1PdfFileId000", lambda r: _media(r, pdf, _pdf("Total 1500")))
    read = await drive.read_file("1PdfFileId000")
    assert read["kind"] == "PDF" and read["content"] == "[Page 1]\nTotal 1500"
    notes = {"id": "1TextFileId00", "name": "notes.md", "mimeType": "text/markdown", "size": "5"}
    fake.on("GET", "/drive/v3/files/1TextFileId00", lambda r: _media(r, notes, "héllo".encode()))
    assert (await drive.read_file("1TextFileId00"))["content"] == "héllo"


def _media(request: httpx.Request, meta: dict, content: bytes) -> httpx.Response:
    """The file's metadata, or its content when asked for it."""
    return (
        httpx.Response(200, content=content)
        if request.url.params.get("alt") == "media"
        else httpx.Response(200, json=meta)
    )


async def test_what_has_no_text_here_says_so(fake):
    for file, meta in {
        "1ImageFileId0": {"id": "1ImageFileId0", "name": "cat.png", "mimeType": "image/png", "size": "10"},
        "1FormFileId00": {"id": "1FormFileId00", "name": "Survey", "mimeType": "application/vnd.google-apps.form"},
        "1HugeFileId00": {"id": "1HugeFileId00", "name": "big.txt", "mimeType": "text/plain", "size": "90000000"},
    }.items():
        fake.on("GET", f"/drive/v3/files/{file}", meta)
    with pytest.raises(GoogleError, match=r"cat.png is a image/png, which can't be read as text here"):
        await drive.read_file("1ImageFileId0")
    with pytest.raises(GoogleError, match="Survey is a Google Form, which can't be read"):
        await drive.read_file("1FormFileId00")
    with pytest.raises(GoogleError, match=r"big.txt is too big to read here \(90 MB\)"):
        await drive.read_file("1HugeFileId00")


async def test_a_sheet_is_read_tab_by_tab(fake):
    meta = {"id": SHEET_ID, "name": "Budget", "mimeType": drive.SPREADSHEET}
    fake.on("GET", f"/drive/v3/files/{SHEET_ID}", meta)
    spreadsheet = {
        "properties": {"title": "Budget"},
        "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        "sheets": [
            {"properties": {"title": "Q4", "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
            {"properties": {"title": "Ana's", "gridProperties": {"rowCount": 10, "columnCount": 3}}},
        ],
    }
    fake.on("GET", f"/v4/spreadsheets/{SHEET_ID}", spreadsheet)
    values = {
        "valueRanges": [
            {"range": "Q4!A1:B2", "values": [["Item", "Cost"], ["Rent", "1500"]]},
            {"range": "'Ana''s'!A1:ZZ500"},
        ]
    }
    fake.on("GET", f"/v4/spreadsheets/{SHEET_ID}/values:batchGet", values)
    read = await drive.read_file(SHEET_ID)
    asked = fake.last("GET", f"/v4/spreadsheets/{SHEET_ID}/values:batchGet").url.params
    assert asked.get_list("ranges") == ["'Q4'!A1:ZZ500", "'Ana''s'!A1:ZZ500"]
    assert read["tabs"] == [{"title": "Q4", "rows": 1000, "columns": 26}, {"title": "Ana's", "rows": 10, "columns": 3}]
    assert read["values"] == [
        {"range": "Q4!A1:B2", "rows": [["Item", "Cost"], ["Rent", "1500"]]},
        {"range": "'Ana''s'!A1:ZZ500", "rows": []},
    ]
    assert read["kind"] == "Google Sheet" and "more" not in read
    await drive.read_sheet(SHEET_ID, "'Q4'!A1:B2")
    assert fake.last("GET", f"/v4/spreadsheets/{SHEET_ID}/values:batchGet").url.params.get_list("ranges") == [
        "'Q4'!A1:B2"
    ]


async def test_a_doc_is_created_from_markdown_in_a_folder(fake):
    fake.on("POST", "/upload/drive/v3/files", {**DOC, "name": "Trip"})
    created = await drive.create_document("Trip", "# Trip\n\n**Day 1**: Rome", folder=FOLDER_ID)
    request = fake.last("POST", "/upload/drive/v3/files")
    assert request.url.params["uploadType"] == "multipart"
    assert request.headers["Content-Type"].startswith("multipart/related; boundary=lo9i-")
    sent = request.content.decode()
    assert f'"name": "Trip", "mimeType": "{drive.DOCUMENT}", "parents": ["{FOLDER_ID}"]' in sent
    assert "Content-Type: text/markdown; charset=UTF-8\r\n\r\n# Trip\n\n**Day 1**: Rome\r\n" in sent
    assert (created["name"], created["link"]) == ("Trip", DOC["webViewLink"])
    # Empty, it's only metadata.
    fake.on("POST", "/drive/v3/files", {**DOC, "name": "Blank"})
    assert (await drive.create_document("Blank"))["name"] == "Blank"
    assert body(fake.last("POST", "/drive/v3/files")) == {"name": "Blank", "mimeType": drive.DOCUMENT}


async def test_appending_starts_a_paragraph_after_what_the_doc_has(fake):
    fake.on("POST", f"/v1/documents/{DOC_ID}:batchUpdate", {"replies": [{}]})
    fake.on(
        "GET", f"/v1/documents/{DOC_ID}", {"title": "Plan", "body": {"content": [{"endIndex": 1}, {"endIndex": 40}]}}
    )
    added = await drive.append_to_document(DOC_ID, "Next: call Ana")
    requests = body(fake.last("POST", f"/v1/documents/{DOC_ID}:batchUpdate"))["requests"]
    assert requests == [{"insertText": {"text": "\nNext: call Ana", "endOfSegmentLocation": {}}}]
    assert added == {"id": DOC_ID, "title": "Plan", "added_characters": 15, "link": DOC["webViewLink"]}
    fake.on("GET", f"/v1/documents/{DOC_ID}", {"title": "New", "body": {"content": [{"endIndex": 1}, {"endIndex": 2}]}})
    await drive.append_to_document(DOC_ID, "First line")
    assert (
        body(fake.last("POST", f"/v1/documents/{DOC_ID}:batchUpdate"))["requests"][0]["insertText"]["text"]
        == "First line"
    )


async def test_replacing_text_says_how_many_changed(fake):
    fake.on("POST", f"/v1/documents/{DOC_ID}:batchUpdate", {"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})
    replaced = await drive.replace_in_document(DOC_ID, "2025", "2026", match_case=False)
    sent = body(fake.last("POST", f"/v1/documents/{DOC_ID}:batchUpdate"))
    assert sent == {
        "requests": [{"replaceAllText": {"containsText": {"text": "2025", "matchCase": False}, "replaceText": "2026"}}]
    }
    assert replaced["replaced"] == 3
    fake.on("POST", f"/v1/documents/{DOC_ID}:batchUpdate", {"replies": [{"replaceAllText": {}}]})
    assert (await drive.replace_in_document(DOC_ID, "nowhere", "x"))["replaced"] == 0
    with pytest.raises(GoogleError, match="Say what text to replace"):
        await drive.replace_in_document(DOC_ID, "", "x")


async def test_rewriting_uploads_markdown_into_the_same_doc(fake):
    fake.on("GET", f"/drive/v3/files/{DOC_ID}", DOC)
    fake.on("PATCH", f"/upload/drive/v3/files/{DOC_ID}", DOC)
    rewritten = await drive.rewrite_document(DOC_ID, "# Plan v2")
    request = fake.last("PATCH", f"/upload/drive/v3/files/{DOC_ID}")
    assert (request.url.params["uploadType"], request.headers["Content-Type"], request.content) == (
        "media",
        "text/markdown",
        b"# Plan v2",
    )
    assert rewritten["id"] == DOC_ID
    fake.on("GET", f"/drive/v3/files/{SHEET_ID}", {"id": SHEET_ID, "name": "Budget", "mimeType": drive.SPREADSHEET})
    with pytest.raises(GoogleError, match="Budget isn't a Google Doc"):
        await drive.rewrite_document(SHEET_ID, "x")


async def test_a_sheet_is_created_with_its_rows_in_a_folder(fake):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    created = {"spreadsheetId": SHEET_ID, "spreadsheetUrl": url, "sheets": [{"properties": {"title": "Costs"}}]}
    fake.on("POST", "/v4/spreadsheets", created)
    fake.on("PUT", f"/v4/spreadsheets/{SHEET_ID}/values/'Costs'!A1", {"updatedRange": "Costs!A1:B2", "updatedCells": 4})
    fake.on("GET", f"/drive/v3/files/{SHEET_ID}", {"parents": ["root-id"]})
    fake.on("PATCH", f"/drive/v3/files/{SHEET_ID}", {"id": SHEET_ID})
    result = await drive.create_spreadsheet("Trip", [["Item", "Cost"], ["Hotel", 300]], tab="Costs", folder=FOLDER_ID)
    assert body(fake.last("POST", "/v4/spreadsheets")) == {
        "properties": {"title": "Trip"},
        "sheets": [{"properties": {"title": "Costs"}}],
    }
    write = fake.last("PUT", f"/v4/spreadsheets/{SHEET_ID}/values/'Costs'!A1")
    assert write.url.params["valueInputOption"] == "USER_ENTERED" and body(write)["values"] == [
        ["Item", "Cost"],
        ["Hotel", 300],
    ]
    move = fake.last("PATCH", f"/drive/v3/files/{SHEET_ID}").url.params
    assert (move["addParents"], move["removeParents"]) == (FOLDER_ID, "root-id")
    assert result == {"id": SHEET_ID, "title": "Trip", "tab": "Costs", "link": url, "updated_range": "Costs!A1:B2"}


async def test_cells_are_written_appended_cleared_and_tabs_added(fake):
    fake.on(
        "PUT",
        f"/v4/spreadsheets/{SHEET_ID}/values/'Q4 budget'!B2",
        {"updatedRange": "'Q4 budget'!B2:C2", "updatedCells": 2},
    )
    written = await drive.write_cells(SHEET_ID, "'Q4 budget'!B2", [["=SUM(A1:A9)", 12.5]])
    request = fake.last("PUT", f"/v4/spreadsheets/{SHEET_ID}/values/'Q4 budget'!B2")
    assert "%27Q4%20budget%27%21B2" in str(request.url) and unquote(request.url.path).endswith("'Q4 budget'!B2")
    assert written == {"id": SHEET_ID, "updated_range": "'Q4 budget'!B2:C2", "cells": 2}
    fake.on(
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values/'Log'!A1:append",
        {"updates": {"updatedRange": "Log!A7:B8", "updatedRows": 2}},
    )
    appended = await drive.append_rows(SHEET_ID, [["a", 1], ["b", 2]], tab="Log")
    params = fake.last("POST", f"/v4/spreadsheets/{SHEET_ID}/values/'Log'!A1:append").url.params
    assert (params["insertDataOption"], params["valueInputOption"]) == ("INSERT_ROWS", "USER_ENTERED")
    assert appended == {"id": SHEET_ID, "updated_range": "Log!A7:B8", "rows": 2}
    fake.on(
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}/values/A1:append",
        {"updates": {"updatedRange": "Sheet1!A3", "updatedRows": 1}},
    )
    assert (await drive.append_rows(SHEET_ID, [["c"]]))["updated_range"] == "Sheet1!A3"
    fake.on("POST", f"/v4/spreadsheets/{SHEET_ID}/values/Log!A2:B9:clear", {"clearedRange": "Log!A2:B9"})
    assert (await drive.clear_cells(SHEET_ID, "Log!A2:B9"))["cleared_range"] == "Log!A2:B9"
    fake.on(
        "POST",
        f"/v4/spreadsheets/{SHEET_ID}:batchUpdate",
        {"replies": [{"addSheet": {"properties": {"sheetId": 7, "title": "2027"}}}]},
    )
    assert await drive.add_tab(SHEET_ID, "2027") == {"id": SHEET_ID, "tab": "2027", "tab_id": 7}
    assert body(fake.last("POST", f"/v4/spreadsheets/{SHEET_ID}:batchUpdate")) == {
        "requests": [{"addSheet": {"properties": {"title": "2027"}}}]
    }
