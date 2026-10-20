# OpenRig source adaptation

Upstream: https://github.com/mvschwarz/openrig

Revision: 0b1d8b3dc1b418f1568e571b1df174041fed77f2

License: Apache License 2.0, retained in LICENSE in this directory.

`jarvis/agents/team_routing.py` is a Python adaptation of
`packages/daemon/src/domain/workflow-role-resolver.ts` at that revision.
Changes: Python data structures and field names, JARVIS ready-state naming,
and compact diagnostic records. The role/liveness/address/runtime gates and
least-pending-work/stable-address ordering come from that source.

Other JARVIS collaboration code is newly implemented. OpenRig's queue ownership,
workflow dependencies and independent reviewer contracts informed its design;
its tmux runtime and complete daemon have not been copied or installed.
