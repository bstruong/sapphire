# Redacted statement fixtures

Committed only after medi-cal-sanitizer JARVIS clean + opaque layout probe.

- `chase_credit_card_sample.pdf` — layout-faithful Chase card PDF with **real household amounts** and tokenized PII (`[TOKEN_N]`). Primary dogfood source of truth for `ChaseCreditCardAdapter`.
- `chase_credit_card_sample.probe.json` — opaque boolean/count probe (safe to open in Cursor).
- `schwab_checking_sample.pdf` — layout-faithful Schwab Bank Investor Checking PDF (real amounts, tokenized PII) for `SchwabCheckingAdapter`.
- `schwab_checking_sample.probe.json` — opaque probe for the Schwab checking fixture.

Do not open the PDF body in Cursor chat. Use QA scripts / adapter error codes / probe JSON only.
