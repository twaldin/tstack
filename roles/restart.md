This session was restarted in place: you ran `tstack restart-self` after files you load changed or memory grew, or liveness restarted it after your wait went quiet. Your system prompt was reloaded from the current files and now wins over everything earlier in this conversation. Where an earlier turn's rules, markers or remembered values differ from the files, follow the files.

Background jobs ended with the old process, your `tstack wait` included. Before anything else:

1. Re-read your seat's `state/`: the stop line in `orders.md` first, then your goal, open gates, unhandled intake, `tasks.tsv` and `lane list`.
2. Treat remembered counters, SHAs, permissions and plans as stale. Read them again from state and GitHub before you act on them.
3. Arm `tstack wait` with the bash tool's `async: true` and `timeout: 0`. Events that arrived while you were down come back in its first batch.
4. Reply with one line naming what you re-read and confirming the wait is armed, then end the turn.
