"""Operator-only interactive OAuth setup. Never launched from an MCP tool."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from google_auth_oauthlib.flow import InstalledAppFlow

from jarvis.features.google.service import SCOPES, token_path


def main():
    parser = argparse.ArgumentParser(description='Connect Gmail metadata and Calendar read-only access.')
    parser.add_argument('--client-secrets', required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.client_secrets.read_text(encoding='utf-8'))
    installed = config.get('installed', {})
    if (installed.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth'
            or installed.get('token_uri') != 'https://oauth2.googleapis.com/token'):
        parser.error('Use a Google OAuth Desktop client with official Google authorization endpoints.')
    destination = token_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(host='127.0.0.1', port=0, timeout_seconds=180,
                                        access_type='offline', prompt='consent',
                                        authorization_prompt_message='',
                                        success_message='JARVIS Google authorization received. You may close this tab.')
    if not credentials.has_scopes(SCOPES):
        parser.error('Required read-only scopes were not granted.')
    # Write atomically. POSIX permissions are restricted; Windows uses inherited directory ACLs.
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(credentials.to_json())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Google read-only credentials saved. Restart J.A.R.V.I.S. if necessary.')


if __name__ == '__main__':
    main()
