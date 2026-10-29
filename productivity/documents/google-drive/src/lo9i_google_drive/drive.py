"""Drive, Docs and Sheets as the signed-in user: finding files, reading them as text, and creating and changing
Google Docs and Sheets. Files are named by their id or their link; what the tools answer has both."""

import io
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from lo9i_google_drive.google import Google, GoogleError, login_from_env

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

FOLDER = "application/vnd.google-apps.folder"
DOCUMENT = "application/vnd.google-apps.document"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
PRESENTATION = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
# What a kind of file is called, for the user and the model; search_files takes these names.
KINDS = {
    "document": DOCUMENT,
    "spreadsheet": SPREADSHEET,
    "presentation": PRESENTATION,
    "folder": FOLDER,
    "pdf": PDF,
    "form": "application/vnd.google-apps.form",
    "drawing": "application/vnd.google-apps.drawing",
}
_NAMES = {
    DOCUMENT: "Google Doc",
    SPREADSHEET: "Google Sheet",
    PRESENTATION: "Google Slides",
    FOLDER: "folder",
    PDF: "PDF",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.drawing": "Google Drawing",
    "application/vnd.google-apps.shortcut": "shortcut",
}
# Google's own files, read as this; Sheets are read through the Sheets API instead.
_EXPORTS = {DOCUMENT: "text/markdown", PRESENTATION: "text/plain"}
# Other files read as text, besides text/*.
_TEXT = {"application/json", "application/xml", "application/javascript", "application/x-yaml", "application/yaml"}
_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,size,owners(displayName,emailAddress),parents,trashed"
# Files bigger than this aren't downloaded to read.
_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_FILES = 50
# A file's text given at once, at most; the rest is read from `start`.
MAX_CHARS = 50_000
# Rows read from each tab of a spreadsheet when no range is given.
SHEET_ROWS = 500
_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_IN_LINK = (re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]{10,})"), re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"))

Cell = str | int | float | bool | None

_google: Google | None = None


def google() -> Google:
    """The signed-in user's Google, made the first time it's needed so its access token is kept between calls.
    Raises GoogleError when the user hasn't signed in."""
    global _google
    if _google is None:
        _google = Google(login_from_env())
    return _google


def file_id(ref: str) -> str:
    """The id in a file's or folder's link, or the id itself; "root" is My Drive. Raises GoogleError."""
    ref = ref.strip()
    if ref == "root" or _ID.match(ref):
        return ref
    for pattern in _IN_LINK:
        if found := pattern.search(ref):
            return found.group(1)
    raise GoogleError(f"{ref!r} isn't a file's id or link: find the file with search_files first.")


async def search_files(
    query: str = "", kind: str = "", folder: str = "", modified_after: str = "", limit: int = 20
) -> dict[str, Any]:
    """The user's files whose name or text has `query`, the latest changed first when there's no query."""
    conditions = ["trashed = false"]
    if query.strip():
        text = _quoted(query.strip())
        conditions.append(f"(name contains {text} or fullText contains {text})")
    if kind:
        if kind not in KINDS:
            raise GoogleError(f"kind must be one of {', '.join(KINDS)}.")
        conditions.append(f"mimeType = '{KINDS[kind]}'")
    if folder:
        conditions.append(f"{_quoted(file_id(folder))} in parents")
    if modified_after:
        conditions.append(f"modifiedTime > {_quoted(modified_after.strip())}")
    params = {
        "q": " and ".join(conditions),
        "pageSize": max(1, min(limit, MAX_FILES)),
        "fields": f"nextPageToken,files({_FIELDS})",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        # Drive doesn't sort a search of the files' text: it gives the best matches first.
        **({} if query.strip() else {"orderBy": "modifiedTime desc"}),
    }
    answer = await google().json("GET", f"{DRIVE}/files", params=params)
    found: dict[str, Any] = {"files": [_file(f) for f in answer.get("files", [])]}
    if answer.get("nextPageToken"):
        found["more"] = "There are more: narrow the search (a kind, a folder, a date) or ask for a higher limit."
    return found


async def read_file(ref: str, start: int = 0) -> dict[str, Any]:
    """A file's text from `start`, at most MAX_CHARS: a Doc as Markdown, a Sheet's tabs as rows, Slides as text,
    a PDF's text, a text file as it is."""
    meta = await _metadata(file_id(ref))
    mime = meta.get("mimeType", "")
    if mime == SPREADSHEET:
        return {**_file(meta), **await read_sheet(meta["id"])}
    if mime == FOLDER:
        return {**_file(meta), **await search_files(folder=meta["id"], limit=MAX_FILES)}
    text = await _text(meta)
    return {**_file(meta), **_part(text, start)}


async def read_sheet(ref: str, cells: str = "") -> dict[str, Any]:
    """A spreadsheet's tabs, and the values in `cells` (A1 notation, such as "Budget!A1:D20"), or in every tab's
    first SHEET_ROWS rows."""
    sheet_id = file_id(ref)
    fields = "properties.title,spreadsheetUrl,sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"
    spreadsheet = await google().json("GET", f"{SHEETS}/{sheet_id}", params={"fields": fields})
    tabs = [s.get("properties", {}) for s in spreadsheet.get("sheets", [])]
    ranges = [cells] if cells.strip() else [f"{_tab(t.get('title', ''))}!A1:ZZ{SHEET_ROWS}" for t in tabs]
    answer = await google().json(
        "GET",
        f"{SHEETS}/{sheet_id}/values:batchGet",
        params=[("ranges", r) for r in ranges] + [("valueRenderOption", "FORMATTED_VALUE")],
    )
    values = [{"range": v.get("range", ""), "rows": v.get("values", [])} for v in answer.get("valueRanges", [])]
    read: dict[str, Any] = {
        "title": spreadsheet.get("properties", {}).get("title", ""),
        "link": spreadsheet.get("spreadsheetUrl", ""),
        "tabs": [
            {
                "title": t.get("title", ""),
                "rows": t.get("gridProperties", {}).get("rowCount", 0),
                "columns": t.get("gridProperties", {}).get("columnCount", 0),
            }
            for t in tabs
        ],
        "values": values,
    }
    if not cells.strip() and any(len(v["rows"]) >= SHEET_ROWS for v in values):
        read["more"] = f"Only the first {SHEET_ROWS} rows of each tab: give cells, like 'Tab'!A501:Z1000, for more."
    return read


async def create_document(title: str, content: str = "", folder: str = "") -> dict[str, Any]:
    """A new Google Doc; `content` is Markdown, which Google turns into the Doc's headings, lists, tables and
    links."""
    metadata = {"name": title, "mimeType": DOCUMENT, **({"parents": [file_id(folder)]} if folder else {})}
    params = {"fields": _FIELDS, "supportsAllDrives": "true"}
    if not content:
        return _file(await google().json("POST", f"{DRIVE}/files", params=params, json=metadata))
    body, boundary = _multipart(metadata, content, "text/markdown")
    created = await google().json(
        "POST",
        f"{UPLOAD}/files",
        params={**params, "uploadType": "multipart"},
        content=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    return _file(created)


async def append_to_document(ref: str, text: str) -> dict[str, Any]:
    """`text` added at the end of a Google Doc, as paragraphs of its own."""
    doc_id = file_id(ref)
    doc = await google().json("GET", f"{DOCS}/{doc_id}", params={"fields": "title,body.content(endIndex)"})
    content = doc.get("body", {}).get("content", [])
    # An empty Doc is one empty paragraph, ending at 2; text after anything else starts a paragraph of its own.
    empty = not content or content[-1].get("endIndex", 0) <= 2
    added = text if empty or text.startswith("\n") else "\n" + text
    insert = {"insertText": {"text": added, "endOfSegmentLocation": {}}}
    await google().json("POST", f"{DOCS}/{doc_id}:batchUpdate", json={"requests": [insert]})
    return {"id": doc_id, "title": doc.get("title", ""), "added_characters": len(added), "link": _doc_link(doc_id)}


async def replace_in_document(ref: str, find: str, replace: str, match_case: bool = True) -> dict[str, Any]:
    """Every `find` in a Google Doc becomes `replace`; their formatting stays."""
    if not find:
        raise GoogleError("Say what text to replace.")
    doc_id = file_id(ref)
    change = {"replaceAllText": {"containsText": {"text": find, "matchCase": match_case}, "replaceText": replace}}
    answer = await google().json("POST", f"{DOCS}/{doc_id}:batchUpdate", json={"requests": [change]})
    replies = answer.get("replies") or [{}]
    changed = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    return {"id": doc_id, "replaced": changed, "link": _doc_link(doc_id)}


async def rewrite_document(ref: str, content: str) -> dict[str, Any]:
    """A Google Doc's whole content replaced by `content`, Markdown. Its name, place, sharing and history stay."""
    doc_id = file_id(ref)
    meta = await _metadata(doc_id)
    if meta.get("mimeType") != DOCUMENT:
        raise GoogleError(f"{meta.get('name', doc_id)} isn't a Google Doc: only those can be rewritten here.")
    updated = await google().json(
        "PATCH",
        f"{UPLOAD}/files/{doc_id}",
        params={"uploadType": "media", "fields": _FIELDS, "supportsAllDrives": "true"},
        content=content.encode(),
        headers={"Content-Type": "text/markdown"},
    )
    return _file(updated)


async def create_spreadsheet(
    title: str, rows: list[list[Cell]] | None = None, tab: str = "", folder: str = ""
) -> dict[str, Any]:
    """A new Google Sheet, with `rows` from A1 of its first tab, named `tab` when given."""
    body: dict[str, Any] = {"properties": {"title": title}}
    if tab:
        body["sheets"] = [{"properties": {"title": tab}}]
    created = await google().json("POST", SHEETS, json=body)
    sheet_id = created["spreadsheetId"]
    first = created.get("sheets", [{}])[0].get("properties", {}).get("title", tab or "Sheet1")
    result: dict[str, Any] = {"id": sheet_id, "title": title, "tab": first, "link": created.get("spreadsheetUrl", "")}
    if rows:
        written = await write_cells(sheet_id, f"{_tab(first)}!A1", rows)
        result["updated_range"] = written["updated_range"]
    if folder:
        await _move(sheet_id, file_id(folder))
    return result


async def write_cells(ref: str, cells: str, rows: list[list[Cell]]) -> dict[str, Any]:
    """`rows` written from the top left of `cells` (A1 notation), as if typed: formulas work, numbers and dates
    are read as such."""
    sheet_id = file_id(ref)
    answer = await google().json(
        "PUT",
        f"{SHEETS}/{sheet_id}/values/{quote(cells, safe='')}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"range": cells, "majorDimension": "ROWS", "values": rows},
    )
    return {"id": sheet_id, "updated_range": answer.get("updatedRange", cells), "cells": answer.get("updatedCells", 0)}


async def append_rows(ref: str, rows: list[list[Cell]], tab: str = "") -> dict[str, Any]:
    """`rows` added under the last row of a tab's table (the first tab's when none is named), as if typed."""
    sheet_id = file_id(ref)
    where = f"{_tab(tab)}!A1" if tab else "A1"
    answer = await google().json(
        "POST",
        f"{SHEETS}/{sheet_id}/values/{quote(where, safe='')}:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"majorDimension": "ROWS", "values": rows},
    )
    updates = answer.get("updates", {})
    return {"id": sheet_id, "updated_range": updates.get("updatedRange", ""), "rows": updates.get("updatedRows", 0)}


async def clear_cells(ref: str, cells: str) -> dict[str, Any]:
    """The values in `cells` (A1 notation) emptied; their formatting stays."""
    sheet_id = file_id(ref)
    answer = await google().json("POST", f"{SHEETS}/{sheet_id}/values/{quote(cells, safe='')}:clear")
    return {"id": sheet_id, "cleared_range": answer.get("clearedRange", cells)}


async def add_tab(ref: str, title: str) -> dict[str, Any]:
    """A new tab at the end of a spreadsheet."""
    sheet_id = file_id(ref)
    request = {"addSheet": {"properties": {"title": title}}}
    answer = await google().json("POST", f"{SHEETS}/{sheet_id}:batchUpdate", json={"requests": [request]})
    replies = answer.get("replies") or [{}]
    properties = replies[0].get("addSheet", {}).get("properties", {})
    return {"id": sheet_id, "tab": properties.get("title", title), "tab_id": properties.get("sheetId")}


async def _metadata(file: str) -> dict[str, Any]:
    params = {"fields": _FIELDS, "supportsAllDrives": "true"}
    return await google().json("GET", f"{DRIVE}/files/{file}", params=params)


async def _text(meta: dict[str, Any]) -> str:
    """The file's text. Raises GoogleError for a kind that has none to read here."""
    file, mime, name = meta["id"], meta.get("mimeType", ""), meta.get("name", "")
    if mime in _EXPORTS:
        response = await google().request("GET", f"{DRIVE}/files/{file}/export", params={"mimeType": _EXPORTS[mime]})
        return response.text
    if mime.startswith("application/vnd.google-apps."):
        raise GoogleError(f"{name} is a {_kind(mime)}, which can't be read as text here: give the user its link.")
    if int(meta.get("size") or 0) > _DOWNLOAD_BYTES:
        raise GoogleError(f"{name} is too big to read here ({int(meta['size']) // 1_000_000} MB).")
    if mime != PDF and not mime.startswith("text/") and mime not in _TEXT:
        raise GoogleError(f"{name} is a {mime or 'file'}, which can't be read as text here: give the user its link.")
    content = (await google().request("GET", f"{DRIVE}/files/{file}", params={"alt": "media"})).content
    return _pdf_text(content, name) if mime == PDF else content.decode("utf-8", errors="replace")


def _pdf_text(content: bytes, name: str) -> str:
    try:
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages]
    except (PdfReadError, ValueError) as e:
        raise GoogleError(f"{name} couldn't be read as a PDF: {e}") from e
    if not any(text.strip() for text in pages):
        raise GoogleError(f"{name} has no text to read: it's scanned images. Give the user its link.")
    return "\n\n".join(f"[Page {n}]\n{text.strip()}" for n, text in enumerate(pages, 1))


def _part(text: str, start: int) -> dict[str, Any]:
    start = max(0, start)
    part: dict[str, Any] = {"content": text[start : start + MAX_CHARS], "characters": len(text)}
    if start + MAX_CHARS < len(text):
        part["more"] = f"Cut at character {start + MAX_CHARS} of {len(text)}: read on with start={start + MAX_CHARS}."
    return part


async def _move(file: str, folder: str) -> None:
    current = await google().json("GET", f"{DRIVE}/files/{file}", params={"fields": "parents"})
    params = {
        "addParents": folder,
        "removeParents": ",".join(current.get("parents", [])),
        "fields": "id",
        "supportsAllDrives": "true",
    }
    await google().json("PATCH", f"{DRIVE}/files/{file}", params=params, json={})


def _file(f: dict[str, Any]) -> dict[str, Any]:
    """What the tools say about a file, without what's empty."""
    owners = f.get("owners") or [{}]
    described = {
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "kind": _kind(f.get("mimeType", "")),
        "modified": f.get("modifiedTime", ""),
        "owner": owners[0].get("emailAddress", ""),
        "size": int(f["size"]) if f.get("size") else None,
        "link": f.get("webViewLink", ""),
    }
    return {k: v for k, v in described.items() if v}


def _kind(mime: str) -> str:
    return _NAMES.get(mime, mime)


def _doc_link(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _quoted(text: str) -> str:
    """A string in a Drive query."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _tab(title: str) -> str:
    """A tab's name in A1 notation."""
    return "'" + title.replace("'", "''") + "'"


def _multipart(metadata: dict[str, Any], content: str, mime: str) -> tuple[bytes, str]:
    """A Drive upload with the file's metadata and its content."""
    boundary = f"lo9i-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n",
        f"--{boundary}\r\nContent-Type: {mime}; charset=UTF-8\r\n\r\n{content}\r\n",
        f"--{boundary}--\r\n",
    ]
    return "".join(parts).encode(), boundary
