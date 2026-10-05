---
name: phone
description: Calling someone for the user with the Phone app on their Mac (the phone MCP server). Use it when the user asks you to call a person or a number.
---

The `phone` MCP server's tools are `mcp__phone__find_contact` and `mcp__phone__call`. A call goes through the user's iPhone; you only start it.

1. If the user gave a number, use it. Otherwise `find_contact` with the name they used.
2. When several contacts match, or the contact has several numbers (mobile, work…), ask which one with `ask_user` before calling. With one match and one number, call it.
3. `call` with the number and the person's name. macOS shows a confirmation on the Mac; the call starts when the user accepts it.
4. Say whom you're calling and that they need to confirm on the Mac. You can't hear or speak on the call: if the user wanted something said, tell them what to say instead.

If `find_contact` says there's no access to Contacts, tell the user where to allow it (the error says). If `call` says the Phone app didn't open, lo9i was probably started at boot, outside their login session: calls need it running in their session.
