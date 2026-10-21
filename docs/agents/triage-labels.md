# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the label strings used on the JARVIS board. The board has no label field, so a label is recorded as a `Triage: <label>` line (first line of `detail`, or a later `finding` note; the latest one wins). See [issue-tracker.md](issue-tracker.md).

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table. A `wontfix` task is also released with board status `cancelled`.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.
