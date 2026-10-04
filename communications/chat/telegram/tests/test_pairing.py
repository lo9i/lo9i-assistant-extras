"""Paired accounts in telegram.json: a code while nobody is paired, codes that work once and expire,
and accounts imported from lo9i's old built-in Telegram."""

import asyncio
import json
from datetime import timedelta

from lo9i_telegram.pairing import FILE, IMPORT_FILE, Pairing


async def test_a_code_is_open_while_nobody_is_paired_and_works_once(tmp_path):
    pairing = await Pairing.load(tmp_path)
    changes = []

    async def changed():
        changes.append(pairing.code)

    pairing.subscribe(changed)
    code = pairing.code
    assert code.startswith("lo9i-")
    assert not await pairing.pair("hi lo9i-0000", 666, "Stranger")
    assert await pairing.pair(f"code {code.upper()}", 222, "Ani")
    assert not await pairing.pair(code, 333, "Other")
    assert pairing.allowed(222) and not pairing.allowed(333) and pairing.code == ""
    assert changes == [""]
    assert json.loads((tmp_path / FILE).read_text()) == {"users": [{"id": 222, "name": "Ani"}]}
    pairing.close()


async def test_paired_accounts_are_kept_and_no_code_opens_by_itself(tmp_path):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": 222, "name": "Ani"}]}))
    pairing = await Pairing.load(tmp_path)
    assert pairing.recipients() == [222] and pairing.code == ""
    assert (await pairing.open()).startswith("lo9i-")
    pairing.close()


async def test_imported_accounts_are_merged_once(tmp_path):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": 222, "name": "Ani"}]}))
    imported = {"users": [{"id": 222, "name": "Ani (old)"}, {"id": 333, "name": "Bob"}]}
    (tmp_path / IMPORT_FILE).write_text(json.dumps(imported))
    pairing = await Pairing.load(tmp_path)
    assert [(u.id, u.name) for u in pairing.users] == [(222, "Ani"), (333, "Bob")]
    assert not (tmp_path / IMPORT_FILE).exists()
    assert len(json.loads((tmp_path / FILE).read_text())["users"]) == 2


async def test_an_expired_code_is_replaced_while_nobody_is_paired(tmp_path):
    pairing = await Pairing.load(tmp_path, lifetime=timedelta(milliseconds=10))
    expired = asyncio.Event()

    async def changed():
        expired.set()

    pairing.subscribe(changed)
    await asyncio.wait_for(expired.wait(), timeout=2)
    assert pairing.code.startswith("lo9i-")
    pairing.close()


async def test_an_expired_code_closes_once_someone_is_paired(tmp_path):
    (tmp_path / FILE).write_text(json.dumps({"users": [{"id": 222, "name": "Ani"}]}))
    pairing = await Pairing.load(tmp_path, lifetime=timedelta(milliseconds=10))
    expired = asyncio.Event()

    async def changed():
        if not pairing.code:
            expired.set()

    await pairing.open()
    pairing.subscribe(changed)
    await asyncio.wait_for(expired.wait(), timeout=2)
    assert pairing.code == ""
