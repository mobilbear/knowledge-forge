# Intent routing

Choose one command family per user turn. The capture family includes the
editorial steps needed to finish a saved source.

## Capture family
- “个人知识库”, “我的知识库”, “LiangWiki”, “我的 Obsidian”, “收藏”,
  “收录”, “入库”, “归档”, “存一下”, and “放进去” are save intent for the
  configured LiangWiki vault; never ask what “个人知识库” means.
- Message contains an http(s) URL and save intent → `capture url`.
  - youtube.com / youtu.be / bilibili / douyin URL → `capture video`.
  - 小红书 / XHS / RED URL → first attempt `capture url`; report queued or
    login-wall limitations truthfully and offer screenshots/video/transcript.
- Message has image attachments of a post/article (小红书截图, screenshot
  threads, 文字长图) → use the gateway-provided local paths with
  `capture images <paths>`; never invent paths.
- Message has a video URL → `capture video` (captions via yt-dlp when
  available). A local video attachment or transcript → `capture file`; local
  video without a transcript is preserved and queued because Whisper is not
  implemented.
- Message attaches a document (.md .txt .pdf .docx) → `capture file`.
- "重试 / retry / try again" about a previously failed item → `capture retry
  <src_id>` using the id from the earlier envelope; if unknown, `kf status
  <keyword>` first to find it.
- A usable capture → follow the editorial workflow in SKILL.md through the
  reading guide, referenced-resource briefing, and current personal edition.

## Query family
- "我收藏过…吗 / 搜索我的个人知识库 / what do I know about / find my notes on" → `search`.
- "刚才那条怎么样了 / did it work" → `status <keyword-or-id>`.

## Digest family
- "今天的简报 / daily brief" → open the latest personal edition and check
  `60_briefings/Current Brief.md` for newer sources.
- "本周总结 / weekly" → use `60_briefings/Current Brief.md` as the evidence index.
- "播客 / 音频 / read it to me" → `podcast build --daily`.

## Maintenance family
- "整理分类 / clean up categories" → `taxonomy garden --dry-run`; present the
  proposals; apply ONLY the specific proposal the user approves via
  `taxonomy apply <id> --approved-by liang`.
- "系统有问题 / check the system" → `doctor`; "修一下" → `doctor --fix-safe`.
- Backup/restore/migrate commands are deployment-session tools; in daily chat
  suggest them but confirm before running `backup create`.

## Confidence rule
If routing confidence is low (ambiguous message, multiple URLs with mixed
intent, or a request outside this table), do NOT guess a mutating command.
Run `kf status`, summarize state, ask one question.
