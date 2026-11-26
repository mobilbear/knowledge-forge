# Error handling

- Envelope `status=failed` → relay `user_message_zh`; suggest `kf doctor`.
  Never retry a mutating command more than once without telling the user.
- "vault is locked" → another operation is running; wait ~1 min, retry once;
  if it persists → `kf doctor` (it detects stale locks) and report.
- "not marked as a Knowledge Forge vault" → STOP. Do not switch vaults or
  stamp anything yourself. Report the exact message to the user; stamping is a
  deliberate user decision (`kf doctor --stamp`).
- Capture returned `queued` (login wall / metadata only) → explain what was
  captured vs missing (from `warnings` / `result.capture_status`), then offer:
  1) retry later (`kf capture retry <src_id>`); 2) user pastes full text;
  3) user sends screenshots → `capture images`.
- Model-related warnings ("fell back to extractive summary", "classification
  pending") are NORMAL degraded operation — content is safe; polish is queued.
  Mention briefly, don't alarm.
- Network totally down → `kf capture` still records evidence and queues
  enrichment (offline mode). Nothing is lost; say so.
- NEVER hide an error to seem successful. A truthful "queued for retry" is a
  good outcome; a fake "已收录" is the one unforgivable failure.
