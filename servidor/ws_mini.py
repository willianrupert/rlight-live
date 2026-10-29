"""WebSocket mínimo (RFC 6455) só com a stdlib, para o voz_server.py continuar sem
dependências: lado servidor (upgrade dentro do http.server, usado pelo terminal S3)
e lado cliente com TLS (conexão com o Gemini Live)."""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Fechado(Exception):
    pass


def _xor(dados, mascara):
    if not dados:
        return dados
    k = (mascara * (len(dados) // 4 + 1))[:len(dados)]
    return (int.from_bytes(dados, "big") ^ int.from_bytes(k, "big")).to_bytes(len(dados), "big")


class WS:
    def __init__(self, leitor, sock, cliente):
        self.r, self.sock = leitor, sock
        self.cliente = cliente        # cliente mascara os frames que envia
        self._lock = threading.Lock()

    def _ler(self, n):
        buf = b""
        while len(buf) < n:
            p = self.r.read(n - len(buf))
            if not p:
                raise Fechado("conexão encerrada")
            buf += p
        return buf

    def receber(self):
        """Próxima mensagem: str (texto) ou bytes (binário). Responde ping sozinho."""
        partes, op_dados = [], None
        while True:
            b1, b2 = self._ler(2)
            op, fin, n = b1 & 0x0F, b1 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._ler(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._ler(8))[0]
            mascara = self._ler(4) if b2 & 0x80 else None
            dados = self._ler(n)
            if mascara:
                dados = _xor(dados, mascara)
            if op == 8:                                   # close
                cod = struct.unpack(">H", dados[:2])[0] if len(dados) >= 2 else 1005
                try:
                    self._frame(8, dados[:2])
                except OSError:
                    pass
                raise Fechado(f"{cod} {dados[2:].decode(errors='replace')}".strip())
            if op == 9:                                   # ping
                self._frame(10, dados)
                continue
            if op == 10:                                  # pong
                continue
            if op in (1, 2):
                op_dados = op
            partes.append(dados)
            if fin:
                msg = b"".join(partes)
                return msg.decode() if op_dados == 1 else msg

    def _frame(self, op, dados):
        cab = bytearray([0x80 | op])
        bit = 0x80 if self.cliente else 0
        n = len(dados)
        if n < 126:
            cab.append(bit | n)
        elif n < 1 << 16:
            cab += bytes([bit | 126]) + struct.pack(">H", n)
        else:
            cab += bytes([bit | 127]) + struct.pack(">Q", n)
        if self.cliente:
            m = os.urandom(4)
            cab += m
            dados = _xor(dados, m)
        with self._lock:
            self.sock.sendall(bytes(cab) + dados)

    def enviar(self, msg):
        if isinstance(msg, str):
            self._frame(1, msg.encode())
        else:
            self._frame(2, bytes(msg))

    def fechar(self, codigo=1000):
        try:
            self._frame(8, struct.pack(">H", codigo))
        except OSError:
            pass
        try:   # destrava a thread que estiver bloqueada em receber()
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def aceitar(handler):
    """Faz o upgrade de um BaseHTTPRequestHandler para WebSocket (lado servidor)."""
    chave = handler.headers.get("Sec-WebSocket-Key", "")
    aceite = base64.b64encode(hashlib.sha1((chave + GUID).encode()).digest()).decode()
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", aceite)
    handler.end_headers()
    handler.close_connection = True
    handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return WS(handler.rfile, handler.connection, cliente=False)


def conectar(url, timeout=10):
    """Abre um WebSocket cliente (wss:// ou ws://)."""
    u = urlsplit(url)
    seguro = u.scheme == "wss"
    sock = socket.create_connection((u.hostname, u.port or (443 if seguro else 80)), timeout=timeout)
    if seguro:
        tls = ssl.create_default_context()
        if not tls.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
            tls.load_verify_locations("/etc/ssl/cert.pem")   # Python do python.org no Mac
        sock = tls.wrap_socket(sock, server_hostname=u.hostname)
    chave = base64.b64encode(os.urandom(16)).decode()
    caminho = (u.path or "/") + (f"?{u.query}" if u.query else "")
    sock.sendall((f"GET {caminho} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {chave}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    r = sock.makefile("rb")
    status = r.readline()
    cabecalhos = []
    while (linha := r.readline()) not in (b"\r\n", b""):
        cabecalhos.append(linha)
    if b" 101 " not in status:
        sock.close()
        raise ConnectionError(f"upgrade recusado: {status.decode(errors='replace').strip()}")
    sock.settimeout(None)
    return WS(r, sock, cliente=True)
