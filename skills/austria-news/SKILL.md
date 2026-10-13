---
name: austria-news
description: Fetch and summarize current Austrian news (national headlines). Use when asked for Austrian news, headlines, or a news brief (e.g. "send me news for austria").
---

# Austrian news

Fetch the current Austrian headlines and reply with a short brief.

## How

1. Fetch the Der Standard RSS feed (works from the container):
   `curl -sL --max-time 20 -A "Mozilla/5.0" https://derstandard.at/feed/1000`
2. Parse the first ~8 `<item>` entries: title, link, optional description.
   (Any quick parsing works: python3 `xml.etree` or grep/sed.)
3. Reply with a compact markdown brief:

   **Austrian news**
   1. <headline> — [source link](url)
   2. …

   Keep it to headlines + one line, no commentary. If the feed cannot be
   fetched, say so and try `https://www.orf.at/` as a fallback (note that
   you could not verify the feed).
