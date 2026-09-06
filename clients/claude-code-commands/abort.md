---
description: Abort a running AI Conclave task by ID, or abort the latest task.
---

Abort the specified task when its elapsed time or token use is no longer worthwhile:

```text
python -m switchboard_conclave --invoked-by claude-code abort <task_id|latest>
```
