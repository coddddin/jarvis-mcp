"""OpenRig role-selection policy adapted for JARVIS native worker seats.

Adapted from mvschwarz/openrig workflow-role-resolver.ts at
0b1d8b3dc1b418f1568e571b1df174041fed77f2 (Apache-2.0).
See third_party/openrig/LICENSE and NOTICE.md. Python port; no tmux probing.
"""


def select_role_seat(role, candidates, runtime=None):
    """Deterministic live-role/runtime selection, then backlog and stable address.

    Mirrors OpenRig's positive eligibility gates. Missing workers are failures,
    never a silent substitution of a different runtime or an operator seat.
    """
    qualified, rejected = [], []
    for candidate in candidates:
        if candidate['node_kind'] != 'agent':
            continue
        reason = None
        if candidate['role'] != role:
            reason = 'role_not_declared'
        elif candidate['state'] != 'ready':
            reason = 'not_live'
        elif not candidate['address']:
            reason = 'coordinate_underivable'
        elif candidate.get('session') not in (None, candidate['address']):
            reason = 'session_identity_mismatch'
        elif runtime is not None and candidate['runtime'] != runtime:
            reason = 'runtime_mismatch'
        if reason:
            rejected.append({'address': candidate['address'], 'reason': reason})
        else:
            qualified.append(candidate)
    qualified.sort(key=lambda candidate: (candidate['pending'], candidate['address']))
    return {'seat': qualified[0]['address'] if qualified else None,
            'qualified': [item['address'] for item in qualified], 'rejected': rejected}
