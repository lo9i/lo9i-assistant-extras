---
name: billing
description: Issuing the user's Argentine electronic invoices (Factura A) with ARCA, ex AFIP, through the billing MCP server; preparing each month's invoices, their PDFs, and emailing them. Use it for anything about invoicing customers, facturas, CAE or ARCA.
---

The invoices live in the `billing` MCP server; its tools are named `mcp__billing__<tool>`. If those tools aren't there, the server is turned off: the user turns it on in MCP servers.

## The model

- **Setup** (`setup`): the issuer (CUIT, razón social, domicilio, IVA condition, which must be responsable inscripto, inicio de actividades, Ingresos Brutos, punto de venta), and for each **environment** the paths of the ARCA certificate and its private key. `homologacion` is ARCA's test environment: its invoices are real-looking but have no fiscal value, and their PDFs say so. `produccion` issues real invoices. `setup` without arguments shows everything and what's `missing`; `check_arca` signs in to ARCA and reads the point of sale's last Factura A, which proves the setup works without issuing anything.
- **Customers** have a CUIT, IVA condition, address, the addresses their invoices are emailed to (`email`, one or more separated by commas) and the email's text (`email_text`), the line their invoices carry (`description`), and a **monthly net amount** with the date it applies from. A raise is `update_customer` with the new `monthly_net` and `effective_from` (a month like `2027-01`): earlier months keep the old amount.
- **Looking a CUIT up**: `lookup_cuit` asks ARCA's padrón (constancia de inscripción) for a CUIT's name, fiscal address and IVA condition. `add_customer` with only `cuit` (no `name`) fills those in from it, so a new customer needs just the CUIT, the monthly amount and the email; tell the user what ARCA said. If the padrón gives no IVA condition, ask the user. It needs the certificate authorized for `ws_sr_padron_a5` too (see Setting up); until then, ask the user for the name, address and IVA condition.
- **Invoices** are Factura A for services (concepto 2), 21% IVA, in pesos, with the service period, the payment due date (issue date + `payment_days`), the CAE and its due date, and a PDF (`pdf_path`). Each month is billed in **two halves**: the 1st to the 15th and the 16th to the month's end, each half the monthly net plus IVA. For 7.500.000 a month: 3.750.000 + 787.500 IVA = 4.537.500 per invoice.

## Issuing

1. `prepare_month` (a `customer` and a `month`; without them, every customer, last month) previews the invoices: periods, amounts, dates, and each one's `status`. It issues nothing.
2. Show the user the preview: customer, period, net, IVA and total of each, and the environment.
3. **In produccion, issue only after the user says yes to that preview** in the conversation. `create_invoice` also asks for approval each time (it's in the plugin's `approve` list), even on a trusted server.
4. Call `create_invoice` once per invoice, with the `create_invoice` arguments from the preview. It returns the invoice with its number, CAE and `pdf_path`.

`create_invoice` never issues two invoices for one customer and period. Calling it again for a period already issued returns that invoice with `already_issued: true`; a period that overlaps an issued one is refused. So when in doubt whether an invoice went through, call `create_invoice` again with the same arguments: never change the period to "try again".

Errors, and what to do:

- `RejectedError`: ARCA said no and nothing was issued. Its messages (code: text) say why; tell the user, fix what can be fixed (a customer's CUIT or IVA condition, a date) and try again. An issue date earlier than the point of sale's last invoice is rejected; so is one more than 10 days from today.
- `UnconfirmedError`: it was sent and no answer came back. Don't issue anything else for that customer and period by other means: call `create_invoice` again later with the same arguments; it asks ARCA first and records the invoice if ARCA has it.
- `AlreadyIssuedError`: that period is already invoiced. An issued invoice can't be changed or deleted; correcting one takes a credit note, which this plugin doesn't issue: the user does it in ARCA.
- `NotReadyError`: the setup is missing something; ask the user for it and call `setup`. When it says the point of sale isn't enabled for web services, it lists the ones that are: ask the user which to use (a Factura A needs one of a responsable inscripto, "Ri Iva") and `setup` it as `point_of_sale`; nothing was issued.
- ARCA may require a Factura de Crédito Electrónica MiPyME (FCE) instead of a Factura A for some large customers and amounts. This plugin doesn't issue FCE: if ARCA's rejection says so, tell the user.

Write amounts the Argentine way ($ 4.537.500,00); tools take and return plain numbers in pesos.

## Emailing an invoice

Emailing is done with the assistant's email (`send_email`), never by this plugin, and **only after the user approves sending it**:

1. `get_invoice` gives the invoice, `pdf_path` (made again if it's missing) and its `email`: `to` (every address the customer has), `subject` and `body` (the customer's email text, or the standard one, filled in for this invoice).
2. Show the user the addresses, the subject and the text, and wait for their yes. They may add addresses or change the text for this email: use what they say. To keep a change for the customer's next invoices, `update_customer` (`email` with all the addresses, separated by commas; `email_text` with the placeholders its description lists).
3. Send it with `send_email`: `to` the addresses (one email to all of them), the subject and body, and the PDF's path (`pdf_path`) in `attachments`. If the file is missing, don't send the email without the invoice: tell the user. If the customer has no address, ask the user for one.

Never email an invoice from homologacion to a customer: its PDF has no fiscal value.

## Every month

When the user wants it, offer a scheduled job on the 1st of each month at 9:00 (`schedule_job`, schedule `0 9 1 * *`), whose task is:

"Use the billing skill. Run mcp__billing__prepare_month for last month. Send the user on Telegram (send_message) each invoice to issue: customer, period, net, IVA, total and the environment. Then call mcp__billing__create_invoice for each one with the arguments the preview gives; each call asks the user to approve it on Telegram, and one not approved is not issued. Finally send the user on Telegram what was issued (number, CAE, total) or why not, and ask whether to email the invoices to the customers; don't email them in this job."

Approving each `create_invoice` happens on Telegram, where the job's approval requests go. When the user answers that they want the invoices emailed, do it in that conversation as in Emailing an invoice, finding them with `list_invoices` (`month`, `status: authorized`).

## Setting up, for the first time

1. In homologacion: the user has a private key (RSA, 2048 bits). With their business details and that key set (`setup` with `key_path`, or the Billing page's Settings), `certificate_request` makes the request (CSR) ARCA issues the certificate for: they paste it in ARCA's testing services (WSASS), authorize the certificate for `wsfe` (invoices) and `ws_sr_padron_a5` (looking CUITs up), and give you the certificate's path. Call `setup` with it, then `check_arca`.
2. Add the customers (`add_customer`), `prepare_month`, and issue one test invoice to see its PDF.
3. For produccion: the user creates a production certificate in ARCA (Administración de Certificados Digitales, uploading the request `certificate_request` makes with `environment: produccion` from that environment's key), authorizes it for "Facturación electrónica" (wsfe) and for "Consulta de constancia de inscripción" (ws_sr_padron_a5) in the Administrador de Relaciones de Clave Fiscal, and creates a point of sale of type "RECE para aplicativo y web services". Then `setup` with `environment: produccion`, that certificate and key, and that `point_of_sale`; `check_arca` again. Switching environments switches which invoices `list_invoices` shows: each environment numbers its own.

Only the files' paths are kept; never ask the user to paste a private key into the chat.

## The Billing page

The user has a **Billing** page in the web app's sidebar: each month's invoices per customer (issued ones with their PDF, the rest with an Issue button that asks them to confirm), the customers and their monthly amounts, and the setup with a Check with ARCA button. Point them there for setting up, adding customers, or issuing by hand; an invoice's `page_path` opens it there.
