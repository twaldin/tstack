---
name: research
description: Investigate a question against primary sources and write the findings to a Markdown file. Use when a topic needs researching, docs or API facts need gathering, or reading legwork can go to a background agent.
---

Hand the reading to a `scout` subagent (omp `task`, agent `scout`) and keep working while it reads. Its brief carries the question, what the answer is for, and where to write the file.

Its job:

1. Investigate the question against **primary sources**: official docs, source code at a named commit, specs, first-party APIs. Follow every claim back to the source that owns it; a blog post or summary is a lead, not a source.
2. Write the findings to one Markdown file. Each claim cites its source as a link or `path:line`; anything not read directly is marked as inferred.
3. Save it where the repo or seat already keeps such notes, matching the existing convention. With none, choose a sensible place and say where.

When it returns, read the file, check two or three citations against their sources, and only then rely on it.
