import http
import logging
import sys
import socket

import certifi
import lz4.frame
import zstandard
from urllib3.poolmanager import PoolManager, ProxyManager
from urllib3.response import HTTPResponse

from clickhouse_connect.driver.exceptions import ProgrammingError

logger = logging.getLogger(__name__)

# Increase this number just to be safe when ClickHouse is returning progress headers
http._MAXHEADERS = 10000  # pylint: disable=protected-access

DEFAULT_KEEP_INTERVAL = 30
DEFAULT_KEEP_COUNT = 3
DEFAULT_KEEP_IDLE = 30

SOCKET_TCP = socket.IPPROTO_TCP

core_socket_options = [
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    (SOCKET_TCP, socket.TCP_NODELAY, 1),
    (socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 256),
    (socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 256)
]

logging.getLogger('urllib3').setLevel(logging.WARNING)
_proxy_managers = {}


# pylint: disable=no-member,too-many-arguments,too-many-branches
def get_pool_manager(keep_interval: int = DEFAULT_KEEP_INTERVAL,
                     keep_count: int = DEFAULT_KEEP_COUNT,
                     keep_idle: int = DEFAULT_KEEP_IDLE,
                     ca_cert: str = None,
                     verify: bool = True,
                     client_cert: str = None,
                     client_cert_key: str = None,
                     http_proxy: str = None,
                     https_proxy: str = None,
                     **options) -> PoolManager:
    socket_options = core_socket_options.copy()
    if getattr(socket, 'TCP_KEEPINTVL', None) is not None:
        socket_options.append((SOCKET_TCP, socket.TCP_KEEPINTVL, keep_interval))
    if getattr(socket, 'TCP_KEEPCNT', None) is not None:
        socket_options.append((SOCKET_TCP, socket.TCP_KEEPCNT, keep_count))
    if getattr(socket, 'TCP_KEEPIDLE', None) is not None:
        socket_options.append((SOCKET_TCP, socket.TCP_KEEPIDLE, keep_idle))
    if sys.platform == 'darwin':
        socket_options.append((SOCKET_TCP, getattr(socket, 'TCP_KEEPALIVE', 0x10), keep_interval))
    options['maxsize'] = options.get('maxsize', 8)
    options['retries'] = options.get('retries', 1)
    if ca_cert == 'certifi':
        ca_cert = certifi.where()
    options['cert_reqs'] = 'CERT_REQUIRED' if verify else 'CERT_NONE'
    if ca_cert:
        options['ca_certs'] = ca_cert
    if client_cert:
        options['cert_file'] = client_cert
    if client_cert_key:
        options['key_file'] = client_cert_key
    if http_proxy:
        if https_proxy:
            raise ProgrammingError('Only one of http_proxy or https_proxy should be specified')
        if not http_proxy.startswith('http'):
            http_proxy = f'http://{http_proxy}'
        return ProxyManager(http_proxy, block=False, socket_options=socket_options, **options)
    if https_proxy:
        if not https_proxy.startswith('http'):
            https_proxy = f'https://{https_proxy}'
        return ProxyManager(https_proxy, block=False, socket_options=socket_options, **options)
    return PoolManager(block=False, socket_options=socket_options, **options)


def get_proxy_manager(host: str, http_proxy):
    key = f'{host}__{http_proxy}'
    if key in _proxy_managers:
        return _proxy_managers[key]
    proxy_manager = get_pool_manager(http_proxy=http_proxy)
    _proxy_managers[key] = proxy_manager
    return proxy_manager


def get_response_data(response: HTTPResponse) -> bytes:
    encoding = response.headers.get('content-encoding', None)
    if encoding == 'zstd':
        try:
            zstd_decom = zstandard.ZstdDecompressor()
            return zstd_decom.stream_reader(response.data).read()
        except zstandard.ZstdError:
            pass
    if encoding == 'lz4':
        lz4_decom = lz4.frame.LZ4FrameDecompressor()
        return lz4_decom.decompress(response.data, len(response.data))
    return response.data


default_pool_manager = get_pool_manager()


class ResponseSource:
    def __init__(self, response: HTTPResponse, chunk_size: int = 1024 * 1024):
        self.response = response
        compression = response.headers.get('content-encoding')
        if compression == 'zstd':
            zstd_decom = zstandard.ZstdDecompressor().decompressobj()

            def decompress():
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    yield zstd_decom.decompress(chunk)

            self.gen = decompress()
        elif compression == 'lz4':
            lz4_decom = lz4.frame.LZ4FrameDecompressor()

            def decompress():
                while lz4_decom.needs_input:
                    data = self.response.read(chunk_size)
                    if lz4_decom.unused_data:
                        data = lz4_decom.unused_data + data
                    if not data:
                        return
                    chunk = lz4_decom.decompress(data)
                    if chunk:
                        yield chunk

            self.gen = decompress()
        else:
            self.gen = response.stream(amt=chunk_size, decode_content=True)

    def close(self):
        self.response.drain_conn()
        self.response.close()
