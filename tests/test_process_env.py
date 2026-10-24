"""Worker environment isolation with synthetic credentials only."""
import json
import os
import subprocess
import sys

import pytest

from jarvis.core.process_env import worker_environment


def test_worker_gets_only_essentials_and_explicit_adapter_settings():
    source = {'Path': '/tools', 'Home': '/profile', 'ComSpec': 'cmd.exe', 'PATHEXT': '.EXE;.CMD',
              'GITHUB_TOKEN': 'synthetic-inbox', 'GH_TOKEN': 'synthetic-gh',
              'OPENAI_API_KEY': 'synthetic-paid', 'ANTHROPIC_API_KEY': 'synthetic-paid',
              'CUSTOM_PRIVATE_VALUE': 'synthetic-private', 'PYTHONPATH': '/injection',
              'NODE_OPTIONS': '--require injection', 'HTTP_PROXY': 'https://user:pass@proxy',
              'OPENCODE_CONFIG_CONTENT': 'untrusted-ambient-config', 'JARVIS_WORKSPACE': '/wrong'}
    before = dict(source)
    env = worker_environment(source=source, overrides={'JARVIS_WORKSPACE': '/trusted'})
    assert env == {'PATH': '/tools', 'HOME': '/profile', 'COMSPEC': 'cmd.exe',
                   'PATHEXT': '.EXE;.CMD', 'JARVIS_WORKSPACE': '/trusted'}
    assert source == before


def test_real_child_cannot_read_parent_credentials():
    source = {'PATH': os.defpath, 'HOME': '/synthetic-home', 'GITHUB_TOKEN': 'synthetic-secret',
              'UNNAMED_SECRET': 'synthetic-secret'}
    child = subprocess.run([sys.executable, '-c', 'import json,os;print(json.dumps(dict(os.environ)))'],
                           env=worker_environment(source=source), capture_output=True, text=True, timeout=10,
                           check=True)
    actual = json.loads(child.stdout)
    assert actual['HOME'] == '/synthetic-home'
    assert 'GITHUB_TOKEN' not in actual and 'UNNAMED_SECRET' not in actual


@pytest.mark.parametrize('overrides', [{'GITHUB_TOKEN': 'synthetic'}, {'PATH': '/replacement'},
                                     {'JARVIS_WORKSPACE': 'bad\x00path'}])
def test_unmanaged_or_invalid_override_rejected(overrides):
    with pytest.raises(ValueError):
        worker_environment(source={}, overrides=overrides)


def test_case_collision_refused_without_exposing_values():
    with pytest.raises(ValueError, match='Ambiguous'):
        worker_environment(source={'PATH': '/first', 'Path': '/second'})
