---
name: expenses
description: Tracking the user's expenses (houses, cars, taxes, bills, what's due or paid this month) with the expenses MCP server. Use it for anything about bills, payments, obligations or monthly spending.
---

The data lives in the `expenses` MCP server; its tools are named `mcp__expenses__<tool>`. Amounts are Argentine pesos. If those tools aren't there, the server is turned off: the user turns it on in MCP servers.

## The model

- **Assets** are things owned that generate expenses: houses, cars (kind `house`, `car` or `other`). Ids are slugs, like `boni`.
- **Obligations** are things paid repeatedly (Luz, ABL, Patente, Monotributo). They belong to an asset or stand alone (taxes). With a `due_day`, a pending bill is created ahead of time for each scheduled month, its amount fixed (`expected_amount`) or estimated from the last real bill.
- **Bills** are single payments. Adding a bill for an obligation and month that already has one updates that bill: that's how a real bill replaces the estimate. A one-off bill needs a `category` instead of an obligation.
- **Categories** are created on first use. Prefer an existing one (`list_categories`).

## How to work

1. Start with `month_summary`: this month's bills, unpaid ones from earlier months, and totals.
2. A bill the user sends (a photo, a PDF, an email, or just "paid the luz, 61.000"): find its obligation with `list_obligations`, then `add_bill` with that `obligation_id`, the amount and the due date. Mark it paid with `update_bill` (`status: paid`) when the user says it's paid.
3. A new recurring expense: `add_obligation` with its `due_day`, how often (`every_months`) and, if the amount is fixed, `expected_amount`.
4. Tool errors list the valid choices; correct the call instead of asking the user.
5. Write amounts the Argentine way: dot for thousands, comma for decimals (45.230,50).

Changes ask the user for approval unless they trust the server. For browsing and editing many bills at once, the user has the **Expenses** page in the web app's sidebar.

## Monthly review

When the user wants a regular review, offer a scheduled job (for example on the 1st at 9:00) whose prompt is: "Use the expenses skill: send me what's due this month, what's still unpaid from earlier months, and the totals by category."
