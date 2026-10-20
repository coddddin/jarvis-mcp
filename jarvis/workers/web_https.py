"""Isolated, bounded HTTPS GET worker. No proxy, cookies, credentials or redirects."""
import base64
import http.client
import ipaddress
import json
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit, parse_qsl

MAX_BYTES = 524288


class FetchError(ValueError):
    pass


def validate_url(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise FetchError('Invalid registered URL.')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment
                or '\\' in url):
            raise FetchError('Only registered HTTPS origins on port 443 are allowed.')
        if any(key.lower() in {'token', 'key', 'secret', 'password', 'code', 'api_key', 'apikey', 'access_token'}
               for key, _ in parse_qsl(parsed.query, max_num_fields=30)):
            raise FetchError('Credential-like URL parameters are forbidden.')
    except ValueError:
        raise FetchError('Invalid registered HTTPS URL.') from None
    return parsed


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or len(addresses) > 32:
        raise FetchError('DNS returned no usable bounded address list.')
    for family, kind, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        mapped = getattr(ip, 'ipv4_mapped', None)
        if (family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global
                or (mapped is not None and not mapped.is_global)
                or getattr(ip, 'sixtofour', None) is not None or getattr(ip, 'teredo', None) is not None):
            raise FetchError('DNS destination is not a public Internet address.')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=4, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the checked numeric address, retaining host SNI/certificate verification.
        family, kind, proto, _, address = self.address
        raw = socket.socket(family, kind, proto)
        raw.settimeout(self.timeout)
        try:
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(url, allowed_hosts):
    parsed = validate_url(url, allowed_hosts)
    addresses = public_addresses(parsed.hostname)
    deadline = time.monotonic() + 8
    last_error = None
    # No unchecked DNS lookup on fallback and at most two connection attempts.
    for address in addresses[:2]:
        connection = PinnedHTTPS(parsed.hostname, address)
        try:
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                headers={'User-Agent': 'JarvisPublicCollector/1.0', 'Accept-Encoding': 'identity',
                         'Accept': 'application/rss+xml, application/atom+xml, application/json, text/html, text/plain, application/xml'})
            response = connection.getresponse()
            if response.status != 200:
                raise FetchError(f'HTTP {response.status}; redirects are not followed.')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise FetchError('Compressed responses are not accepted.')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                raise FetchError('Response size is invalid or exceeds limit.')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError('HTTPS read deadline exceeded.')
                if connection.sock is not None:
                    connection.sock.settimeout(min(4, remaining))
                chunk = response.read1(min(32768, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FetchError('Response exceeds size limit.')
                chunks.append(chunk)
            return {'content_type': response.getheader('Content-Type', '')[:200],
                    'body_base64': base64.b64encode(b''.join(chunks)).decode('ascii')}
        except FetchError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        finally:
            connection.close()
    raise FetchError('HTTPS request failed (' + str(last_error) + ').')


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(8193))
        result = fetch(request['url'], request['allowed_hosts'])
        print(json.dumps({'ok': True, **result}))
    except FetchError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'Public HTTPS collection failed.'}))


if __name__ == '__main__':
    main()
