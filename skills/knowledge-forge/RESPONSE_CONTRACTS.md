# Response contracts

Every `kf --json` call returns:

```json
{"schema_version": "1.0", "command": "capture.url", "run_id": "run_...",
 "status": "completed|queued|needs_review|failed|noop",
 "result": {...}, "warnings": [], "errors": [],
 "needs_user_action": false, "user_message_zh": "已收录……"}
```

Rules for you:
- Speak `user_message_zh` (translate to the user's language if needed).
- `warnings` worth one short sentence each; `errors` verbatim if the user asks.
- `status=queued` → the item did NOT enter the knowledge base; never imply it did.
- `status=noop` → duplicate; tell the user it was already captured, with the
  existing note path from `result.existing_note`.
- `needs_user_action=true` → end your reply with the single concrete action
  the user can take (paste text, approve category, run retry).
- Never summarize `result.diff` unless asked; it exists for audit.

Useful `result` fields after capture: `note` (vault path), `source_id`,
`capture_status`, `content_completeness`, `primary_category_id`,
`pending_note`.
