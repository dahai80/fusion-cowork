# issue #585-#588: demand engine M1 mail rail (unified lead pipeline).
# Adapters (poll_qq_imap) -> normalize_lead -> match -> dual_draft.
# Pure-code scoring (no LLM in match, Rule 5); mail content is untrusted
# data everywhere downstream (quoted-data wrapper in drafts).
