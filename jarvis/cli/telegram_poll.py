"""PA4-T1: answer "how is the work I handed over going?" from the user's phone over Telegram. Read only.

Outbound HTTPS long polling (getUpdates) to api.telegram.org only: no webhook, no listening port. Replies only to
one private chat whose user id and chat id were paired locally. Reads the board (tasks created by user:heejun) and
the dispatcher's observed state; never writes the board except a fixed alert line, never runs commands.

The bot token is a Windows DPAPI (CurrentUser) blob written by scripts/telegram/setup-secrets.ps1 and decrypted
here with ctypes (no pywin32). It lives only in memory and in the request URL; exceptions, logs, the board and the
ledger never carry it.

Usage (operator-started, foreground; nothing registers a service):
  python -m jarvis.cli.telegram_poll check   # local setup check, no network
  python -m jarvis.cli.telegram_poll pair    # pair the phone with a one-time code shown here
  python -m jarvis.cli.telegram_poll poll    # answer /status and /task until stopped
"""
import argparse
import ctypes
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, ValidationError

from jarvis.agents import alerts, dispatch_control, telegram_view
from jarvis.agents.dispatch_service import read_pause
from jarvis.agents.telegram_state import DIRECTORY, TelegramState
from jarvis.app.runtime import load_guardrails
from jarvis.core.guardrails import GuardrailError
from jarvis.features.board.service import BoardError, BoardStore

API = 'https://api.telegram.org'
CONFIG = '.jarvis-telegram/config.json'
LOCK = '.jarvis-telegram/poll.lock'
CREATOR = 'user:heejun'   # v1: only tasks the user created; other sources become a config option later
SENDER = 'local:telegram'
USER = 'user:heejun'
ENTROPY = b'jarvis-telegram-v1'
TOKEN = re.compile(r'[0-9]{5,16}:[A-Za-z0-9_-]{30,64}')
POLL_SECONDS = 25
MIN_GAP_SECONDS = 5
MAX_PER_HOUR = 30
SEND_ATTEMPTS = 5
SEND_WINDOW_SECONDS = 600
MAX_BACKOFF = 300
MAX_BODY = 1_000_000
PAIR_SECONDS = 300
PAIR_MAX_WRONG = 10


class TelegramError(Exception):
    """kind: network, server, rate, token_invalid, blocked, conflict, bad_request or invalid_response.
    Carries no URL, token or response text."""

    def __init__(self, kind, retry_after=None):
        super().__init__(kind)
        self.kind, self.retry_after = kind, retry_after


class Stop(Exception):
    """The poller must stop; code is a fixed reason."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class TokenError(Exception):
    pass


class TelegramConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = False
    allowed_user_id: int | None = None
    allowed_chat_id: int | None = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('redirect refused')   # the token is in the URL path: never follow it elsewhere


def classify(status, body):
    """HTTP status -> TelegramError. The Bot API says error_code may change, so the HTTP status decides; 409 is not
    described on the Bot API page and is treated as a conflict that stops polling."""
    retry_after = None
    if isinstance(body, dict) and isinstance(body.get('parameters'), dict):
        value = body['parameters'].get('retry_after')
        if type(value) is int and value > 0:
            retry_after = min(value, 3600)
    if status in (401, 404):
        return TelegramError('token_invalid')
    if status == 403:
        return TelegramError('blocked')
    if status == 409:
        return TelegramError('conflict')
    if status == 429:
        return TelegramError('rate', retry_after or 30)
    if 400 <= status < 500:
        return TelegramError('bad_request')
    return TelegramError('server')


class HttpTransport:
    """POST JSON to one Bot API method. Ambient proxies and redirects are refused."""

    def __init__(self, token, *, opener=None):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise TokenError('Bot token format is invalid.')
        self._token = token
        self._opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def __repr__(self):
        return 'HttpTransport(<token hidden>)'

    def call(self, method, params, *, timeout=15):
        request = Request(f'{API}/bot{self._token}/{method}', data=json.dumps(params).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        # The error is raised after the except blocks, so it carries no __context__ (the HTTPError holds the URL,
        # and the URL holds the token).
        failure, raw = None, None
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
        except HTTPError as exc:
            try:
                body = json.loads(exc.read(65536))
            except (OSError, ValueError, HTTPException):
                body = None
            failure = classify(exc.code, body)
        except (URLError, OSError, ValueError, HTTPException):   # HTTPException: IncompleteRead, BadStatusLine
            failure = TelegramError('network')
        if failure is not None:
            raise failure
        if len(raw) > MAX_BODY:
            raise TelegramError('invalid_response')
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get('ok') is not True or 'result' not in body:
            raise TelegramError('invalid_response')
        return body['result']


# ---- token (DPAPI) -------------------------------------------------------------------------------------------

def token_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise TokenError('LOCALAPPDATA is not set.')
    return Path(base) / 'JarvisTelegram' / 'bot-token.bin'


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def unprotect(data, entropy=ENTROPY):
    """CryptUnprotectData for the current Windows user, no UI. Raises TokenError on any failure."""
    if os.name != 'nt':
        raise TokenError('DPAPI is only available on Windows.')
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, keep1 = _blob(data)
    extra, keep2 = _blob(entropy)
    result = _Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x1,
                                      ctypes.byref(result)):
        raise TokenError('Bot token could not be decrypted for this Windows user.')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)
        del keep1, keep2


def load_token(path=None):
    path = path or token_path()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise TokenError('Bot token file is missing or unusual. Run scripts/telegram/setup-secrets.ps1.')
        plain = unprotect(path.read_bytes())
    except OSError:
        raise TokenError('Bot token file could not be read.') from None
    # isascii first: a UnicodeDecodeError would keep the decrypted bytes in the exception context.
    token = plain.decode('ascii') if plain.isascii() else ''
    if not TOKEN.fullmatch(token):
        raise TokenError('Bot token format is invalid.')
    return token


# ---- config ---------------------------------------------------------------------------------------------------

def load_config(guard):
    path = guard.validate_path(CONFIG, must_exist=False)
    if not path.exists():
        return TelegramConfig()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise GuardrailError('Unusual Telegram config is refused.')
    try:
        return TelegramConfig.model_validate_json(path.read_bytes())
    except ValidationError:
        raise GuardrailError('Telegram config is invalid.') from None


def save_config(guard, config):
    directory = guard.validate_path(DIRECTORY, must_exist=False)
    if directory.is_symlink() or directory.is_junction():
        raise GuardrailError('Linked Telegram storage is forbidden.')
    directory.mkdir(exist_ok=True)
    path = guard.validate_path(CONFIG, must_exist=False)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(config.model_dump_json(indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _int(value):
    return type(value) is int


def sender_ids(message):
    """(user id, chat id) of a private text message from a person, else None."""
    if not isinstance(message, dict):
        return None
    chat, user = message.get('chat'), message.get('from')
    if not isinstance(chat, dict) or not isinstance(user, dict) or chat.get('type') != 'private':
        return None
    if user.get('is_bot') is not False or not _int(user.get('id')) or not _int(chat.get('id')):
        return None
    return user['id'], chat['id']


def allowed(message, config):
    ids = sender_ids(message)
    return (ids is not None and config.allowed_user_id is not None and config.allowed_chat_id is not None
            and ids == (config.allowed_user_id, config.allowed_chat_id))


def _updates(result):
    """Well-formed updates only, sorted; the highest update_id of the whole batch moves the cursor."""
    if not isinstance(result, list):
        raise TelegramError('invalid_response')
    items = [item for item in result if isinstance(item, dict) and _int(item.get('update_id'))]
    highest = max((item['update_id'] for item in items), default=None)
    return sorted(items, key=lambda item: item['update_id']), highest


# ---- poller ---------------------------------------------------------------------------------------------------

class Poller:
    def __init__(self, guard, transport, state, *, config, read_status, read_task, alert,
                 clock=time.time, sleep=time.sleep):
        self.guard, self.transport, self.state = guard, transport, state
        self.config, self.read_status, self.read_task, self.alert = config, read_status, read_task, alert
        self.clock, self.sleep = clock, sleep

    def _stop(self, code):
        self.alert(code)
        raise Stop(code)

    def check_webhook(self):
        try:
            info = self.transport.call('getWebhookInfo', {})
        except TelegramError as exc:
            if exc.kind == 'token_invalid':
                self._stop('token_invalid')
            raise
        if not isinstance(info, dict) or info.get('url'):
            self._stop('webhook_set')   # getUpdates does not work while a webhook is set; never delete it here

    def fetch(self, timeout):
        params = {'timeout': timeout, 'allowed_updates': ['message']}
        offset = self.state.offset()
        if offset is not None:
            params['offset'] = offset
        return _updates(self.transport.call('getUpdates', params, timeout=timeout + 10))

    def handle(self, updates, highest):
        if highest is None:
            return
        now = self.clock()
        seen = self.state.seen(item['update_id'] for item in updates)
        fresh = [item for item in updates if item['update_id'] not in seen]
        last, hour, skipped = self.state.recent_answers(now)
        entries, questions = [], []   # questions: allowed text messages, in update order

        for item in fresh:
            message = item.get('message')
            entry = {'update_id': item['update_id']}
            if not allowed(message, self.config):
                entries.append({**entry, 'outcome': 'rejected'})
            elif not isinstance(message.get('text'), str) or not _int(message.get('date')):
                entries.append({**entry, 'outcome': 'ignored'})
            else:
                questions.append(item)
        if questions:
            entries += self.answer(questions, now, last=last, hour=hour, skipped=skipped)
        self.state.record(entries, highest)

    def answer(self, questions, now, *, last, hour, skipped):
        """At most one reply per batch. Questions sent while the PC or poller was off (older than
        BACKLOG_SECONDS) turn the reply into one status summary that also covers the newer questions."""
        late = [item for item in questions if now - item['message']['date'] > telegram_view.BACKLOG_SECONDS]
        if hour >= MAX_PER_HOUR or (not late and last is not None and now - last < MIN_GAP_SECONDS):
            return [{'update_id': item['update_id'], 'outcome': 'rate_limited'} for item in questions]
        status = lambda: telegram_view.status_reply(self.read_status(now), now, self.read_service())  # noqa: E731
        note = f'\n(빈도 제한으로 답하지 않은 질문 {skipped}건)' if skipped else ''
        if late:
            oldest = min(item['message']['date'] for item in late)
            text = telegram_view.backlog_prefix(len(late), oldest, now) + '\n' + status()
            chosen, outcome, other = late[-1], 'backlog', 'backlog_merged'
        else:
            chosen, outcome, other = questions[0], 'answered', 'rate_limited'
            text = self.reply(chosen['message']['text'], now, status)
        entries = [{'update_id': item['update_id'], 'outcome': other} for item in questions if item is not chosen]
        entries.append({'update_id': chosen['update_id'], 'outcome': outcome,
                        'chat_id': chosen['message']['chat']['id'], 'text': telegram_view.finish(text + note)})
        return entries

    def reply(self, text, now, status):
        command, argument = telegram_view.parse(text)
        if command == 'status':
            return status()
        if command == 'task':
            return telegram_view.task_reply(self.read_task(argument), now, self.read_service())
        if command == 'bad_task':
            return telegram_view.finish('사용법: /task <ID 앞 6자리 이상, 16진수>')
        return telegram_view.finish(telegram_view.HELP)

    def read_service(self):
        try:
            return {'status': dispatch_control.service_state(self.guard)['status'],
                    'paused': read_pause(self.guard)['paused']}
        except Exception:  # observation only: an unreadable record is shown as unknown
            return None

    def deliver(self):
        now = self.clock()
        for row in self.state.due(now):
            if (row['chat_id'] != self.config.allowed_chat_id or row['attempts'] >= SEND_ATTEMPTS
                    or now - row['created'] > SEND_WINDOW_SECONDS):
                self.state.expire(row['id'])
                continue
            try:
                self.transport.call('sendMessage', {'chat_id': row['chat_id'], 'text': row['text']})
            except TelegramError as exc:
                if exc.kind == 'token_invalid':
                    self._stop('token_invalid')
                if exc.kind == 'blocked':
                    self.state.expire_chat(row['chat_id'])
                    self.alert('blocked')
                    return
                if exc.kind == 'bad_request':
                    self.state.expire(row['id'])
                    continue
                delay = exc.retry_after if exc.kind == 'rate' else min(MAX_BACKOFF, 5 * 2 ** row['attempts'])
                self.state.retry(row['id'], now + delay)
                return   # keep order; try the rest on the next round
            self.state.sent(row['id'])

    def run(self, *, rounds=None, enabled=lambda: True):
        """Poll until stopped. rounds bounds the loop for tests. Returns the stop code."""
        self.check_webhook()
        backoff, done = 5, 0
        while rounds is None or done < rounds:
            done += 1
            self.guard.check_running()
            if not enabled():
                return 'disabled'
            try:
                timeout = 1 if self.state.pending_count() else POLL_SECONDS
                self.handle(*self.fetch(timeout))
                backoff = 5
            except TelegramError as exc:
                if exc.kind in ('token_invalid', 'conflict'):
                    self._stop(exc.kind)
                wait = exc.retry_after if exc.kind == 'rate' else backoff
                backoff = min(MAX_BACKOFF, backoff * 2)
                self.sleep(wait)
            self.deliver()
        return 'rounds_done'


def pair(guard, transport, state, *, code, confirm, clock=time.time, seconds=PAIR_SECONDS):
    """Wait for a private message equal to code. confirm((user_id, chat_id)) decides locally. Returns
    TelegramConfig or None. Every update read here is recorded, so it is never answered later."""
    end, wrong = clock() + seconds, 0
    while clock() < end and wrong < PAIR_MAX_WRONG:
        guard.check_running()
        params = {'timeout': max(1, min(POLL_SECONDS, int(end - clock()))), 'allowed_updates': ['message']}
        offset = state.offset()
        if offset is not None:
            params['offset'] = offset
        updates, highest = _updates(transport.call('getUpdates', params, timeout=params['timeout'] + 10))
        if highest is None:
            continue
        seen, entries, found = state.seen(item['update_id'] for item in updates), [], None
        for item in updates:
            if item['update_id'] in seen:
                continue
            message, ids = item.get('message'), sender_ids(item.get('message'))
            text = message.get('text') if isinstance(message, dict) else None
            # Bytes, not str: compare_digest raises TypeError on non-ASCII str, which would crash on every rerun.
            if (found is None and ids and isinstance(text, str)
                    and secrets.compare_digest(text.strip().encode('utf-8'), code.encode('ascii'))):
                found = ids
                entries.append({'update_id': item['update_id'], 'outcome': 'pairing'})
            else:
                wrong += 1
                entries.append({'update_id': item['update_id'], 'outcome': 'rejected'})
        state.record(entries, highest)
        if found is not None:
            if not confirm(found):
                return None
            return TelegramConfig(enabled=True, allowed_user_id=found[0], allowed_chat_id=found[1])
    return None


# ---- wiring ---------------------------------------------------------------------------------------------------

def make_alert(guard, board, clock=time.time):
    def alert(code):
        try:
            alerts.notify(alerts.AlertLedger(guard), alerts.events(telegram=[code]),
                          lambda text: board.send(SENDER, USER, text), clock())
        except Exception:  # the stop itself still happens; only the board line is missing
            print(f'telegram: alert not recorded ({code})', file=sys.stderr)
    return alert


def board_readers(board):
    def read_status(now):
        try:
            return board.created_by(CREATOR, closed_since=now - 86400)
        except (BoardError, GuardrailError, sqlite3.Error, OSError):
            return None

    def read_task(prefix):
        try:
            return board.created_by(CREATOR, prefix=prefix)['matches']
        except (BoardError, GuardrailError, sqlite3.Error, OSError):
            return None
    return read_status, read_task


def acquire_lock(guard):
    """One poller per workspace (two getUpdates loops would conflict). Held until the process exits."""
    path = guard.validate_path(LOCK, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - non-Windows hosts
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise Stop('already_running') from None
    return handle


def _check(guard):
    config = load_config(guard)
    try:
        load_token()
        token = 'ok'
    except TokenError as exc:
        token = str(exc)
    return {'paired': config.allowed_user_id is not None and config.allowed_chat_id is not None,
            'enabled': config.enabled, 'token': token, 'network': 'not contacted'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='자비스 PA4-T1: 휴대폰 Telegram으로 맡긴 일 상태를 조회만 한다.')
    parser.add_argument('action', choices=['check', 'pair', 'poll'])
    args = parser.parse_args(argv)
    try:
        guard = load_guardrails()
        if args.action == 'check':
            print(json.dumps(_check(guard), ensure_ascii=False))
            return 0
        lock = acquire_lock(guard)
        try:
            transport, state = HttpTransport(load_token()), TelegramState(guard)
            board = BoardStore(guard)
            if args.action == 'pair':
                code = f'{secrets.randbelow(10 ** 6):06d}'
                print(f'휴대폰에서 봇에게 이 숫자만 보내세요 (5분): {code}', flush=True)

                def confirm(ids):
                    answer = input(f'받은 메시지: user id {ids[0]}, chat id {ids[1]}. 이 계정을 허용할까요? (y/N) ')
                    return answer.strip().lower() == 'y'
                Poller(guard, transport, state, config=TelegramConfig(), read_status=None, read_task=None,
                       alert=make_alert(guard, board)).check_webhook()
                config = pair(guard, transport, state, code=code, confirm=confirm)
                if config is None:
                    print('연결하지 않았습니다.')
                    return 1
                save_config(guard, config)
                print('연결했습니다. 이제 poll로 조회를 시작할 수 있습니다.')
                return 0
            config = load_config(guard)
            if not config.enabled or config.allowed_user_id is None or config.allowed_chat_id is None:
                print('먼저 pair로 휴대폰을 연결하세요 (또는 config의 enabled가 false).')
                return 1
            read_status, read_task = board_readers(board)
            poller = Poller(guard, transport, state, config=config, read_status=read_status, read_task=read_task,
                            alert=make_alert(guard, board))
            print('Telegram 조회 시작. 이 창을 닫으면 멈춥니다.', flush=True)
            result = poller.run(enabled=lambda: load_config(guard).enabled)
            print(f'멈춤: {result}')
            return 0
        finally:
            lock.close()
    except Stop as exc:
        print(f'멈춤: {exc.code}', file=sys.stderr)
        return 2
    except (TokenError, GuardrailError, BoardError, TelegramError) as exc:
        print(f'실패: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
