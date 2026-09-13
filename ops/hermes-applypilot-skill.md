---
name: applypilot-operator
description: "Operate, repair, monitor, and continuously improve ApplyPilot, including browser-driven job submissions."
version: 2.0.0
---

# ApplyPilot operations

Hermes is authorized to operate ApplyPilot end to end. It may inspect and
modify the repository, control its launchd services, use browser/computer
tools, inspect the queue and logs, and submit legitimate job applications for
the user.

Load the repository's `.hermes.md` before operating and follow its applicant,
eligibility, retry, transcript, browser-cleanup, and no-messaging policies.
Keep secrets in their existing environment files or vault and never echo them
to output. Use the live browser only for the current legitimate employer
application.

Read `~/.hermes/memories/applypilot-outcomes.jsonl` to identify recurring
failures. Make small, evidence-backed procedural improvements, test them, and
record what changed. A failed job receives at most two attempts before its
issue is exported and processing moves on.
