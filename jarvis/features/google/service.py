"""Read-only Google briefing. Credentials belong to this server, not Codex connectors."""
from jarvis.core.paths import AUTH_ROOT
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
import requests

SCOPES = (
    'https://www.googleapis.com/auth/gmail.metadata',
    'https://www.googleapis.com/auth/calendar.events.readonly',
)
TOKEN_FILE = AUTH_ROOT / 'google-token.json'
SEOUL = timezone(timedelta(hours=9), 'Asia/Seoul')


class GoogleIntegrationError(ValueError):
    """Actionable error without credential or API response disclosure."""


def token_path() -> Path:
    value = os.environ.get('JARVIS_GOOGLE_TOKEN_FILE')
    path = Path(value) if value else TOKEN_FILE
    if not path.is_absolute():
        raise GoogleIntegrationError('Google token path must be absolute.')
    return path


class GoogleBriefing:
    def __init__(self):
        self.lock = threading.Lock()

    def status(self) -> dict:
        # File presence is deliberately not reported as an authenticated connection.
        return {'credentials_present': token_path().is_file(), 'connection_verified': False,
                'mode': 'read-only Gmail metadata and primary Calendar events',
                'setup': 'Run google_auth_setup.py with a Google Desktop OAuth client JSON.'}

    def _session(self) -> AuthorizedSession:
        path = token_path()
        if not path.is_file():
            raise GoogleIntegrationError('Google not connected. Run google_auth_setup.py first.')
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(info, dict) or not isinstance(info.get('scopes'), list):
                raise GoogleIntegrationError('Invalid credentials structure.')
            if set(info.get('scopes', [])) != set(SCOPES):
                raise GoogleIntegrationError('Token scopes differ from the read-only policy; reconnect.')
            if info.get('token_uri') != 'https://oauth2.googleapis.com/token':
                raise GoogleIntegrationError('Unsupported OAuth token endpoint.')
            credentials = Credentials.from_authorized_user_info(info, scopes=SCOPES)
            return AuthorizedSession(credentials, refresh_timeout=10, max_refresh_attempts=1)
        except (OSError, ValueError, TypeError) as exc:
            raise GoogleIntegrationError('Invalid Google credentials; reconnect using setup helper.') from exc

    @staticmethod
    def _get(session, url: str, params: dict) -> dict:
        try:
            response = session.get(url, params=params, timeout=10, allow_redirects=False)
            if response.status_code != 200:
                raise GoogleIntegrationError(
                    f'Google API returned HTTP {response.status_code}; check consent and enabled APIs.')
            data = response.json()
            if not isinstance(data, dict):
                raise GoogleIntegrationError('Unexpected Google API response.')
            return data
        except (requests.RequestException, GoogleAuthError, ValueError) as exc:
            if isinstance(exc, GoogleIntegrationError):
                raise
            raise GoogleIntegrationError('Google request failed or authentication expired; reconnect if needed.') from exc

    def briefing(self, day: str = '', limit: int = 10) -> dict:
        if type(limit) is not int or not 1 <= limit <= 10:
            raise GoogleIntegrationError('Limit must be an integer from 1 to 10.')
        if day and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise GoogleIntegrationError('Date must be YYYY-MM-DD.')
        try:
            target = date.fromisoformat(day) if day else datetime.now(SEOUL).date()
        except ValueError as exc:
            raise GoogleIntegrationError('Invalid calendar date.') from exc
        start = datetime.combine(target, time.min, tzinfo=SEOUL)
        result = {'date': target.isoformat(), 'timezone': 'Asia/Seoul',
                  'generated_at': datetime.now(SEOUL).isoformat(),
                  'untrusted_content': True, 'partial': False,
                  'mail_scope': 'Current unread inbox metadata, not limited to selected calendar date.'}
        with self.lock, self._session() as session:
            # Both services report their own failures; an unavailable service is never an empty success.
            try:
                events = self._get(session, 'https://www.googleapis.com/calendar/v3/calendars/primary/events', {
                    'timeMin': start.isoformat(), 'timeMax': (start + timedelta(days=1)).isoformat(),
                    'timeZone': 'Asia/Seoul', 'singleEvents': 'true', 'orderBy': 'startTime',
                    'maxResults': limit,
                    'fields': 'items(id,summary,start,end,htmlLink),nextPageToken',
                })
                result['calendar'] = {'status': 'ok', 'events': events.get('items', []),
                                      'has_more': bool(events.get('nextPageToken'))}
            except GoogleIntegrationError as exc:
                result['calendar'] = {'status': 'error', 'error': str(exc)}
                result['partial'] = True
            try:
                messages = self._get(session, 'https://gmail.googleapis.com/gmail/v1/users/me/messages', {
                    'labelIds': ['INBOX', 'UNREAD'], 'maxResults': limit,
                    'fields': 'messages(id),nextPageToken,resultSizeEstimate',
                })
                items = []
                for item in messages.get('messages', [])[:limit]:
                    message_id = item.get('id', '')
                    if not re.fullmatch(r'[a-zA-Z0-9_-]+', message_id):
                        raise GoogleIntegrationError('Unexpected Gmail message identifier.')
                    metadata = self._get(session,
                        'https://gmail.googleapis.com/gmail/v1/users/me/messages/' + message_id, {
                            'format': 'metadata', 'metadataHeaders': ['From', 'Subject', 'Date'],
                            'fields': 'id,payload(headers)',
                        })
                    headers = {header['name'].lower(): header.get('value', '')[:1000]
                               for header in metadata.get('payload', {}).get('headers', [])}
                    items.append({'id': message_id, 'from': headers.get('from', ''),
                                  'subject': headers.get('subject', ''), 'date': headers.get('date', '')})
                result['gmail'] = {'status': 'ok', 'messages': items,
                                   'has_more': bool(messages.get('nextPageToken')),
                                   'unread_estimate': messages.get('resultSizeEstimate', 0)}
            except GoogleIntegrationError as exc:
                result['gmail'] = {'status': 'error', 'error': str(exc)}
                result['partial'] = True
        return result
