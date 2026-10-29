"""API keys in Windows Credential Manager (generic credentials of the current user, kept on this PC only).

Windows protects the stored blob for the signed-in user; JARVIS keeps no copy on disk. Callers name an entry
(a connection_ref such as 'JARVIS/brain/openai-api') and read the secret only when a request needs it. No method
returns a secret except get(), and no error text ever contains one.

API: CredWriteW / CredReadW / CredDeleteW / CredFree in advapi32, CREDENTIALW with CRED_TYPE_GENERIC (1) and
CRED_PERSIST_LOCAL_MACHINE (2); the blob is at most CRED_MAX_CREDENTIAL_BLOB_SIZE (5*512) bytes.
https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw
"""
import ctypes
from ctypes import wintypes
import re
import sys

NAME = re.compile(r'JARVIS/[a-z0-9-]{1,32}/[a-z0-9-]{1,32}')
SECRET = re.compile(r'[\x21-\x7e]{8,512}')          # printable ASCII without spaces: every API key format we use
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
ERROR_NOT_FOUND = 1168


class CredentialError(Exception):
    """The credential store failed. The message never includes the secret."""


def valid_name(name):
    return isinstance(name, str) and NAME.fullmatch(name) is not None


def valid_secret(secret):
    return isinstance(secret, str) and SECRET.fullmatch(secret) is not None


class _FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR), ('LastWritten', _FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


class WindowsCredentials:
    """Generic credentials of the current Windows user. On other systems every call raises CredentialError."""

    def __init__(self):
        self._api = None

    def _advapi(self):
        if sys.platform != 'win32':
            raise CredentialError('Windows 자격 증명 관리자는 Windows에서만 쓸 수 있습니다.')
        if self._api is None:
            api = ctypes.WinDLL('advapi32', use_last_error=True)
            api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
            api.CredWriteW.restype = wintypes.BOOL
            api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
            api.CredReadW.restype = wintypes.BOOL
            api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            api.CredDeleteW.restype = wintypes.BOOL
            api.CredFree.argtypes = [ctypes.c_void_p]
            api.CredFree.restype = None
            self._api = api
        return self._api

    def set(self, name, secret):
        if not valid_name(name) or not valid_secret(secret):
            raise CredentialError('키 형식이 올바르지 않습니다.')
        api = self._advapi()
        data = secret.encode('ascii')
        if len(data) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            raise CredentialError('키가 너무 깁니다.')
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        credential = _CREDENTIALW(Type=CRED_TYPE_GENERIC, TargetName=name, Comment='JARVIS',
                                  CredentialBlobSize=len(data), CredentialBlob=blob,
                                  Persist=CRED_PERSIST_LOCAL_MACHINE, UserName='JARVIS')
        try:
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialError(f'키를 저장하지 못했습니다(오류 {ctypes.get_last_error()}).')
        finally:
            ctypes.memset(blob, 0, len(data))

    def get(self, name):
        """The secret, or None when there is no entry with this name."""
        if not valid_name(name):
            raise CredentialError('연결 이름이 올바르지 않습니다.')
        api = self._advapi()
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not api.CredReadW(name, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == ERROR_NOT_FOUND:
                return None
            raise CredentialError(f'키를 읽지 못했습니다(오류 {code}).')
        try:
            entry = pointer.contents
            data = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize) if entry.CredentialBlobSize else b''
        finally:
            api.CredFree(pointer)
        try:
            secret = data.decode('ascii')
        except UnicodeDecodeError:
            return None
        return secret if valid_secret(secret) else None

    def delete(self, name):
        """True when an entry was removed, False when there was none."""
        if not valid_name(name):
            raise CredentialError('연결 이름이 올바르지 않습니다.')
        api = self._advapi()
        if api.CredDeleteW(name, CRED_TYPE_GENERIC, 0):
            return True
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return False
        raise CredentialError(f'키를 지우지 못했습니다(오류 {code}).')
