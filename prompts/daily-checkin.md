Daily check-in for {{CURRENT_DATE}} ({{CURRENT_WEEKDAY}}).

Step 1: Ask me exactly 3 short questions about my day, numbered, in one message, then stop and wait for my answers. Make them specific to me, using my notes:
1. What actually happened or got done today.
2. Health and energy (sleep, training, food), tied to my own protocol.
3. My single most important open item from my notes (name it), and whether it moved.
No other text in Step 1.

Step 2: After I answer, draft today's Daily note as ONE markdown code block, matching my vault's daily template exactly:

```
---
type: daily
area: meta
status: done
updated: {{CURRENT_DATE}}
up: "[[Home]]"
---
# {{CURRENT_DATE}} ({{CURRENT_WEEKDAY}})

> Daily log. Keep it short — 3-5 bullets per section, not a full report.

## What happened
- ...

## Decisions made
- ...

## Open items
- [ ] ... #area/<personal|school|jobs|dealerscope|trading|fbm>

## Notes
- ...
```

Rules for the draft: write in first person as me. "What happened" and "Notes" contain ONLY what I said in my answers, with no inferences and no status of items I didn't mention. Open items may add relevant carried-forward tasks from my notes, copied as written. Invent nothing. Use 3-5 bullets per section at most, and write "- (none)" if a section is empty. Carry forward any open item I said didn't move. After the code block, add exactly one line: "Paste into Daily/{{CURRENT_DATE}}.md in Obsidian." Then show my full open to-do list from my notes (newest dated status wins; drop items marked done), grouped by area. You can't write to the vault yourself.
