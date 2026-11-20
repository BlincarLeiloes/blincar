"""Leitor de placas da Blin Car.

Olha a câmera do portão, lê as placas dos carros que passam e manda para a planilha (que marca a chegada e a saída
na agenda). Fica rodando no computador da oficina.

Uso (os arquivos .bat da pasta fazem isso):
  python leitor.py               liga o leitor (fica rodando)
  python leitor.py --procurar    acha a câmera na rede e guarda o endereço dela
  python leitor.py --testar X    lê as placas de uma foto ou vídeo (não manda nada)
  python leitor.py --preparar    baixa os arquivos da leitura de placas (uma vez, na instalação)
  python leitor.py --verificar   confere a configuração, a câmera e a planilha
  python leitor.py --tela        lê as placas na janela do "CMS of EseeCloud" (quando a câmera não manda o vídeo direto)
  python leitor.py --area        abre a imagem da câmera para marcar com o mouse onde os carros passam
  python leitor.py --ponto       marca o lugar onde vocês mostram a placa na mão
  python leitor.py --modo        escolhe o que conta: placa mostrada na mão ou carros entrando e saindo
"""
import argparse
import asyncio
import base64
import concurrent.futures
import fractions
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import placas

VERSAO = "1.4"
PASTA = Path(__file__).resolve().parent
ARQ_CONFIG = PASTA / "config-blincar.json"
ARQ_FILA = PASTA / "fila.jsonl"
PASTA_FOTOS = PASTA / "fotos"
PORTA_TRAVA = 47811  # só um leitor ligado por vez

PADRAO = {
    "planilha": "",
    "chave": "",
    "camera": "",
    # Parte da imagem onde o carro passa, em frações (0 a 1): [esquerda, topo, direita, baixo]. Vazio = imagem toda.
    "area": [],
    "detector": "yolo-v9-t-640-license-plate-end2end",
    "ocr": "cct-xs-v2-global-model",
    "analises_por_segundo": 4,
    "segundos_fim_passagem": 4,
    "min_leituras": 2,
    "confianca_minima": 0.55,
    "minutos_mesma_placa": 2,
    # Só conta carro andando (entrando ou saindo). Carro estacionado na frente da câmera não é lido de novo.
    "so_carro_andando": True,
    # O que conta: "mao" (a placa mostrada na mão para a câmera) ou "carros" (carros entrando e saindo pelo portão).
    "contar": "mao",
    # Modo "mao": o lugar da imagem onde vocês mostram a placa (frações, como a area; vazio = qualquer lugar)
    # e o tamanho mínimo da placa (fração da largura da imagem): placa de carro lá no fundo é menor e não conta.
    "ponto": [],
    "placa_na_mao": 0.05,
    "segundos_mao": 2,
    # Câmera ao vivo no painel: vídeo direto (WebRTC) e, se não der, uma imagem atrás da outra pela planilha.
    "ao_vivo": True,
    "stun": ["stun:stun.l.google.com:19302"],
    "dias_guardar_fotos": 30,
    "minutos_ping": 5,
    "segundos_fila": 60,
}

log = logging.getLogger("leitor")


def preparar_saida():
    """Acentos no Prompt de Comando do Windows."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def configurar_log(console=True):
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%d/%m %H:%M:%S")
    arq = logging.handlers.RotatingFileHandler(PASTA / "leitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    arq.setFormatter(fmt)
    log.addHandler(arq)
    if console and sys.stdout:
        tela = logging.StreamHandler(sys.stdout)
        tela.setFormatter(fmt)
        log.addHandler(tela)


# ---------- Configuração ----------

def pasta_downloads():
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


def config_baixada():
    """O config-blincar.json mais novo que o painel baixou (o navegador pode chamar de "config-blincar (1).json")."""
    try:
        arqs = sorted(pasta_downloads().glob("config-blincar*.json"), key=lambda a: a.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    for arq in arqs:
        try:
            dados = json.loads(arq.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if dados.get("chave") and dados.get("planilha"):
            return dados
    return {}


def ler_config():
    """config-blincar.json da pasta do leitor, com o endereço da planilha e a chave do que o painel baixou por último
    (na pasta Downloads). Não precisa mexer em nenhum dos dois arquivos."""
    cfg = dict(PADRAO)
    if ARQ_CONFIG.exists():
        try:
            cfg.update(json.loads(ARQ_CONFIG.read_text(encoding="utf-8-sig")))
        except Exception as e:
            raise SystemExit(f"O arquivo {ARQ_CONFIG.name} está com defeito ({e}). Apague ele e baixe de novo no painel: aba Câmera.")
    baixada = config_baixada()
    if baixada and (baixada["chave"] != cfg.get("chave") or baixada["planilha"] != cfg.get("planilha")):
        cfg.update(planilha=baixada["planilha"], chave=baixada["chave"])
        salvar_config(cfg)
        print("Peguei a configuração que o painel baixou (pasta Downloads).")
    return cfg


def salvar_config(cfg):
    # Estas ficam sempre no arquivo (assim o leitor ligado percebe quando mudam, mesmo voltando ao padrão).
    guardar = {k: v for k, v in cfg.items() if k in ("planilha", "chave", "camera", "area", "contar", "ponto") or v != PADRAO.get(k)}
    ARQ_CONFIG.write_text(json.dumps(guardar, ensure_ascii=False, indent=2), encoding="utf-8")


def faltando(cfg):
    falta = []
    if not re.match(r"^(https://|http://(localhost|127\.0\.0\.1)[:/])", str(cfg.get("planilha", ""))):
        falta.append("o endereço da planilha")
    if len(str(cfg.get("chave", ""))) < 20:
        falta.append("a chave da câmera")
    return falta


# ---------- Planilha ----------

def enviar(cfg, corpo, tempo=30):
    """Manda para a planilha (Apps Script). Devolve a resposta (dict) ou levanta erro se não deu para falar com ela."""
    dados = json.dumps(dict(corpo, chave=cfg["chave"], versao=VERSAO)).encode("utf-8")
    req = urllib.request.Request(cfg["planilha"], data=dados, headers={"Content-Type": "text/plain;charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=tempo) as r:
        texto = r.read().decode("utf-8", errors="replace")
    try:
        return json.loads(texto)
    except ValueError:
        raise IOError("a planilha respondeu algo estranho (confira o endereço no config-blincar.json)")


class Envio(threading.Thread):
    """Manda as leituras para a planilha. Sem internet, guarda em fila.jsonl e tenta de novo a cada minuto."""

    def __init__(self, cfg, estado):
        super().__init__(daemon=True)
        self.cfg, self.estado = cfg, estado
        self.fila = queue.Queue()
        self.parar = threading.Event()
        self.chave_errada = False

    def mandar(self, evento):
        self.fila.put(evento)

    def _guardar(self, evento):
        with open(ARQ_FILA, "a", encoding="utf-8") as f:
            f.write(json.dumps(evento, ensure_ascii=False) + "\n")

    def _pendentes(self):
        if not ARQ_FILA.exists():
            return []
        linhas = [l for l in ARQ_FILA.read_text(encoding="utf-8").splitlines() if l.strip()]
        ARQ_FILA.unlink()
        return [json.loads(l) for l in linhas]

    def _tentar(self, evento, avisar=True):
        try:
            r = enviar(self.cfg, evento)
        except Exception as e:
            if avisar:
                log.info(f"Sem conexão com a planilha ({e}). Guardei a leitura de {evento.get('placa', '')} para mandar depois.")
            self._guardar(evento)
            return False
        if r.get("ok") is False and r.get("erro") == "chave":
            if not self.chave_errada:
                log.info("A planilha não aceitou a chave. No painel, aba Câmera > \"Baixar a configuração de novo\" e troque o config-blincar.json.")
            self.chave_errada = True
            return True
        self.chave_errada = False
        if evento.get("acao") == "camera_evento":
            log.info(f"Planilha: {evento['placa']} -> {r.get('tipo', r)}")
        return True

    def run(self):
        # Primeiro aviso 20 segundos depois de ligar (dá tempo de conectar na câmera); depois a cada 5 minutos
        # ou na hora em que a câmera cai ou volta.
        ultimo_ping = time.time() - self.cfg["minutos_ping"] * 60 + 20
        ultima_fila, camera_avisada = 0.0, None
        while not self.parar.is_set():
            agora = time.time()
            mudou = camera_avisada is not None and camera_avisada != self.estado["camera_ok"]
            if mudou or agora - ultimo_ping >= self.cfg["minutos_ping"] * 60:
                ultimo_ping = agora
                camera_avisada = self.estado["camera_ok"]
                try:
                    r = enviar(self.cfg, {"acao": "camera_ping", "camera": self.estado["camera_ok"], "erro": self.estado["camera_erro"],
                                          "modo": self.cfg.get("contar", "mao"), "fps": self.estado.get("fps", 0)})
                    if r.get("ok") is False and r.get("erro") == "chave":
                        log.info("A planilha não aceitou a chave. Baixe a configuração de novo no painel (aba Câmera).")
                except Exception as e:
                    log.info(f"Sem conexão com a planilha ({e}).")
            if agora - ultima_fila >= self.cfg["segundos_fila"]:
                ultima_fila = agora
                pendentes = self._pendentes()
                for i, ev in enumerate(pendentes):
                    if not self._tentar(ev, avisar=False):
                        # Continua sem conexão: as que faltam voltam para a fila, na mesma ordem.
                        for resto in pendentes[i + 1:]:
                            self._guardar(resto)
                        break
                    if i == 0:
                        log.info(f"Conexão de volta: mandando {len(pendentes)} leitura(s) guardada(s).")
            try:
                ev = self.fila.get(timeout=1)
            except queue.Empty:
                continue
            self._tentar(ev)


# ---------- Câmera ----------

def abrir_video(url, tempo_ms=6000):
    """A câmera como um VideoCapture do OpenCV (isOpened, read, release). O endereço pode ser:
    rtsp://... ou um arquivo de vídeo (OpenCV); bubble://... (câmeras da EseeCloud); tela:TÍTULO (janela do CMS)."""
    if str(url).startswith("bubble://"):
        return Bubble(url, tempo_ms / 1000)
    if str(url).startswith("tela:"):
        return Tela(url[5:])
    import cv2
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, tempo_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, tempo_ms])
    except TypeError:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    return cap


# ---------- Vídeo direto das câmeras da EseeCloud (o mesmo jeito que o "CMS of EseeCloud" e o app IP Pro usam) ----------
# Essas câmeras (Juan / dvr163 / EseeCloud) nem sempre têm RTSP. Elas mandam o vídeo num formato próprio, o "bubble":
# pede GET /bubble/live, manda usuário e senha, pede o vídeo e recebe os quadros em H.264 (ou H.265).
# Endereço: bubble://admin:SENHA@IP:PORTA/bubble/live?ch=0&stream=0   (stream 0 = imagem grande, 1 = pequena)

class ErroCamera(Exception):
    pass


SENHA_ERRADA = "A câmera não aceitou a senha. É a mesma senha que vocês usam no CMS of EseeCloud."
RESPOSTAS_LOGIN = {}  # "ip:porta" -> o começo da resposta da câmera ao login (anotado no procurar.log)


class Bubble:
    """Lê o vídeo "bubble" e devolve as imagens como o OpenCV (umas 8 por segundo, sempre a mais nova)."""

    LOGIN, VIDEO, ABRIR, ABRIR_CANAL, CHEIA = 0x00, 0x01, 0x0A, 0x04, 0x08

    def __init__(self, url, tempo=6.0, conectar=True):
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        self.ip, self.porta = u.hostname or "", u.port or 80
        self.usuario = urllib.parse.unquote(u.username or "") or "admin"
        self.senha = urllib.parse.unquote(u.password or "")
        self.canal = int((q.get("ch") or ["0"])[0] or 0)
        self.fluxo = int((q.get("stream") or ["0"])[0] or 0)
        self.caminho = u.path or "/bubble/live"
        self.tempo = tempo
        self.sock = self.arq = self.dec = None
        self.erro, self.senha_errada, self.morto = "", False, False
        self.login_duvidoso = self.viu_video = False
        self.entregue = 0.0
        if conectar:
            try:
                self._conectar()
            except ErroCamera as e:
                self.erro = str(e)
                self.release()
            except OSError as e:
                self.erro = f"A câmera não respondeu em {self.ip}:{self.porta} ({e})."
                self.release()

    def _mandar(self, comando, carimbo, dados):
        # 0xAA + tamanho (4) + comando (1) + carimbo (4) + dados (+ 4 bytes zerados, como o programa deles manda)
        self.sock.sendall(b"\xaa" + struct.pack(">I", 5 + len(dados)) + bytes([comando]) + struct.pack(">I", carimbo) + dados + bytes(4))

    def _ler(self, n):
        dados = self.arq.read(n)
        if len(dados) < n:
            raise ErroCamera("A câmera fechou a conexão.")
        return dados

    def _pacote(self, antes=None):
        """(comando, dados) do próximo pacote. "antes": guarda o que vier antes do pacote (a descrição em XML)."""
        b = self._ler(1)
        while b != b"\xaa":
            if antes is None or len(antes) > 16384:
                raise ErroCamera("Essa porta não é o vídeo da câmera.")
            antes += b
            b = self._ler(1)
        cab = self._ler(9)
        tamanho = struct.unpack(">I", cab[:4])[0]
        if not 5 <= tamanho <= 16_000_000:
            raise ErroCamera("A câmera mandou algo que não entendi.")
        return cab[4], self._ler(tamanho - 5)

    def _conectar(self):
        self.sock = socket.create_connection((self.ip, self.porta), timeout=self.tempo)
        self.sock.settimeout(self.tempo)
        self.arq = self.sock.makefile("rb", buffering=262144)
        self.sock.sendall(f"GET {self.caminho}?ch={self.canal}&stream={self.fluxo} HTTP/1.1\r\n\r\n".encode())
        primeira = self.arq.readline(512)
        if not re.match(rb"HTTP/1\.\d 200", primeira):
            raise ErroCamera("Essa porta não é o vídeo da câmera.")
        for _ in range(50):
            linha = self.arq.readline(2048)
            if not linha:
                raise ErroCamera("A câmera fechou a conexão.")
            if linha in (b"\r\n", b"\n"):
                break
            nome, _, valor = linha.decode("latin-1").partition(":")
            if nome.strip().lower() == "content-type" and "bubble" not in valor.lower():
                raise ErroCamera("Essa porta não é o vídeo da câmera.")
        # Depois do cabeçalho vem uma descrição em XML (completada com "#") e a câmera espera o login.
        b = struct.pack(">I", 44) + bytes(4) + self.usuario.encode()[:20].ljust(20, b"\0") + self.senha.encode()[:20].ljust(20, b"\0")
        self._mandar(self.LOGIN, 0x0E16C271, b)
        xml = bytearray()
        comando, r = self._pacote(xml)
        if comando == self.CHEIA:
            raise ErroCamera("A câmera não aceita mais ninguém vendo agora. Feche o vídeo dela no celular e espere 1 minuto.")
        if comando != self.LOGIN or len(r) < 9 or r[4] != 3:
            raise ErroCamera("A câmera respondeu o login de um jeito que não entendi.")
        RESPOSTAS_LOGIN[f"{self.ip}:{self.porta}"] = r[:16].hex(" ")
        if r[8] != 1:
            # Senha errada, ou um firmware que responde o login de outro jeito: pede o vídeo mesmo assim.
            # Se o vídeo não vier, aí sim é a senha (ver read).
            self.login_duvidoso = True
        m = re.search(rf"<stream{self.fluxo} [^>]+".encode(), bytes(xml))
        h265 = bool(m and b".265" in m.group(0))
        try:
            import av
        except ImportError:
            raise ErroCamera("Falta uma parte do leitor (av). Rode o instalar.bat de novo.")
        self.dec = av.CodecContext.create("hevc" if h265 else "h264", "r")
        # Pede o vídeo (esse pedaço é em little-endian mesmo): canal, imagem, ligado, reservado.
        self._mandar(self.ABRIR, 0x0E16C2DF, struct.pack("<4I", self.canal, self.fluxo, 1, 0))
        if self.fluxo == 0:
            # Firmwares mais antigos só entendem o pedido antigo (canal, ligado). Vai separado para a câmera
            # ler um de cada vez; quem entende os dois só abre a mesma imagem de novo.
            time.sleep(0.3)
            self._mandar(self.ABRIR_CANAL, 0x0E16C2DF, bytes([self.canal & 0xFF, 1]))

    def isOpened(self):
        return self.sock is not None and not self.morto

    def get(self, _prop):
        return 0

    def read(self):
        if not self.isOpened():
            return False, None
        import av
        try:
            while True:
                comando, p = self._pacote()
                if comando == self.CHEIA:
                    raise ErroCamera("A câmera não aceita mais ninguém vendo agora. Feche o vídeo dela no celular e espere 1 minuto.")
                # Vídeo: tamanho (4) + tipo (1: quadro inteiro, 2: só o que mudou, 0: som) + canal (1) + o quadro.
                if comando != self.VIDEO or len(p) < 7 or p[4] == 0:
                    continue
                self.viu_video = True
                try:
                    quadros = self.dec.decode(av.Packet(p[6:]))
                except av.error.FFmpegError:
                    continue
                if quadros and time.time() - self.entregue >= 0.08:
                    self.entregue = time.time()
                    return True, quadros[-1].to_ndarray(format="bgr24")
        except (OSError, ErroCamera) as e:
            self.erro = str(e) if isinstance(e, ErroCamera) else "A câmera parou de mandar imagem."
            sem_resposta = isinstance(e, OSError) or "fechou a conexão" in self.erro
            if not self.viu_video and sem_resposta:
                if self.login_duvidoso:
                    self.senha_errada = True
                    self.erro = SENHA_ERRADA
                else:
                    self.erro = "A câmera aceitou a senha, mas não mandou o vídeo."
            self.morto = True
            return False, None

    def release(self):
        for x in (self.arq, self.sock):
            try:
                if x is not None:
                    x.close()
            except OSError:
                pass
        self.sock = self.arq = None


def resposta_bubble(ip, porta, tempo=2.5):
    """Essa porta manda o vídeo "bubble" das câmeras da EseeCloud? (pede o vídeo e vê se responde "video/bubble")."""
    dados = b""
    try:
        with socket.create_connection((ip, porta), timeout=tempo) as s:
            s.settimeout(tempo)
            s.sendall(b"GET /bubble/live?ch=0&stream=0 HTTP/1.1\r\n\r\n")
            while len(dados) < 2048 and b"<bubble" not in dados:
                parte = s.recv(2048)
                if not parte:
                    break
                dados += parte
    except OSError:
        pass
    return bool(re.match(rb"HTTP/1\.\d 200", dados)) and (b"video/bubble" in dados.lower() or b"<bubble" in dados)


# ---------- Ler a tela do "CMS of EseeCloud" (quando a câmera não manda o vídeo direto) ----------

TITULO_CMS = "CMS of EseeCloud"
# Para abrir o CMS sozinho se ele for fechado (ou depois de reiniciar): espera 30 s sem a janela e
# não tenta de novo antes de 10 minutos.
_CMS = {"sumiu": 0.0, "abriu": 0.0, "programa": "", "esperar": 30, "de_novo": 600}


def windows():
    return os.name == "nt"


_WIN = []


def _win():
    """user32 e gdi32 com os tipos certos (sem isso os endereços de 64 bits são cortados)."""
    if _WIN:
        return _WIN[0]
    import ctypes
    from ctypes import wintypes as W
    u, g = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True)
    tipos = {
        (u, "GetDC"): ([W.HWND], W.HDC), (u, "ReleaseDC"): ([W.HWND, W.HDC], ctypes.c_int),
        (u, "PrintWindow"): ([W.HWND, W.HDC, W.UINT], W.BOOL), (u, "IsIconic"): ([W.HWND], W.BOOL),
        (u, "IsWindowVisible"): ([W.HWND], W.BOOL), (u, "GetWindowTextLengthW"): ([W.HWND], ctypes.c_int),
        (u, "GetWindowTextW"): ([W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
        (u, "GetClientRect"): ([W.HWND, ctypes.POINTER(W.RECT)], W.BOOL),
        (u, "ClientToScreen"): ([W.HWND, ctypes.POINTER(W.POINT)], W.BOOL),
        (u, "GetWindowThreadProcessId"): ([W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
        (g, "CreateCompatibleDC"): ([W.HDC], W.HDC), (g, "CreateCompatibleBitmap"): ([W.HDC, ctypes.c_int, ctypes.c_int], W.HBITMAP),
        (g, "SelectObject"): ([W.HDC, W.HGDIOBJ], W.HGDIOBJ), (g, "DeleteObject"): ([W.HGDIOBJ], W.BOOL),
        (g, "DeleteDC"): ([W.HDC], W.BOOL),
        (g, "BitBlt"): ([W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL),
        (g, "GetDIBits"): ([W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p, W.UINT], ctypes.c_int),
    }
    for (dll, nome), (args, volta) in tipos.items():
        f = getattr(dll, nome)
        f.argtypes, f.restype = args, volta
    _WIN.append((u, g))
    return u, g


def preparar_tela(acordado=True):
    """Windows: imagem no tamanho real (sem a escala de 125%/150%) e a tela sem apagar enquanto o leitor roda."""
    if not windows():
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if acordado:
        try:  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED (vale enquanto esta parte do leitor roda)
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
        except Exception:
            pass


def achar_janelas(titulo):
    """[(janela, título)] das janelas visíveis com esse pedaço no título."""
    import ctypes
    from ctypes import wintypes as W
    u, _ = _win()
    achadas = []
    tipo = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    @tipo
    def cada(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if titulo.lower() in buf.value.lower():
                    achadas.append((hwnd, buf.value))
        return True

    u.EnumWindows.argtypes = [tipo, W.LPARAM]
    u.EnumWindows(cada, 0)
    return achadas


def _bitmap(g, hdc, bmp, w, h):
    import ctypes
    import numpy as np
    from ctypes import wintypes as W

    class Cab(ctypes.Structure):  # BITMAPINFO: o cabeçalho (40 bytes) e uma sobra para as cores
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                    ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG),
                    ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD), ("cores", W.DWORD * 4)]
    cab = Cab(40, w, -h, 1, 32, 0)  # 32 bits por ponto, sem compressão; -h: de cima para baixo
    buf = ctypes.create_string_buffer(w * h * 4)
    if g.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(cab), 0) != h:
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()


def _copiar_janela(hwnd, w, h, da_tela):
    """A parte de dentro da janela: pedindo para ela se desenhar (funciona até com outra janela na frente)
    ou copiando o que está na tela (precisa estar aparecendo)."""
    import ctypes
    from ctypes import wintypes as W
    u, g = _win()
    origem = u.GetDC(None if da_tela else hwnd)
    hdc = g.CreateCompatibleDC(origem)
    bmp = g.CreateCompatibleBitmap(origem, w, h)
    velho = g.SelectObject(hdc, bmp)
    try:
        if da_tela:
            p = W.POINT(0, 0)
            u.ClientToScreen(hwnd, ctypes.byref(p))
            ok = g.BitBlt(hdc, 0, 0, w, h, origem, p.x, p.y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
        else:
            ok = u.PrintWindow(hwnd, hdc, 1 | 2) or u.PrintWindow(hwnd, hdc, 1)  # só a parte de dentro + vídeo (DirectX)
        g.SelectObject(hdc, velho)
        return _bitmap(g, hdc, bmp, w, h) if ok else None
    finally:
        g.SelectObject(hdc, velho)
        g.DeleteObject(bmp)
        g.DeleteDC(hdc)
        u.ReleaseDC(None if da_tela else hwnd, origem)


def _preta(img):
    return img is None or (float(img.mean()) < 2 and int(img.max()) < 16)


def capturar_tela(titulo):
    """(imagem, erro) da janela com esse título (a do CMS of EseeCloud)."""
    import ctypes
    from ctypes import wintypes as W
    if not windows():
        return None, "Ler a tela só funciona no Windows."
    u, _ = _win()
    janelas = achar_janelas(titulo)
    if not janelas:
        return None, f'Não achei a janela "{titulo}". Abra o CMS of EseeCloud com o vídeo da câmera aparecendo.'

    def tamanho(j):
        r = W.RECT()
        u.GetClientRect(j[0], ctypes.byref(r))
        return (r.right - r.left) * (r.bottom - r.top)
    hwnd = max(janelas, key=tamanho)[0]
    if u.IsIconic(hwnd):
        return None, "A janela do CMS está minimizada. Clique nela na barra de tarefas (pode ter outra janela na frente, só não minimize)."
    r = W.RECT()
    u.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 100 or h < 100:
        return None, "A janela do CMS está pequena demais. Maximize ela."
    img = _copiar_janela(hwnd, w, h, False)
    if _preta(img):
        img = _copiar_janela(hwnd, w, h, True)
    if img is None:
        return None, "Não consegui copiar a imagem da janela do CMS."
    if _preta(img):
        return None, ("A imagem do CMS está preta. Confira se o vídeo da câmera está tocando no CMS "
                      "e se a tela do computador está ligada e desbloqueada.")
    return img, ""


def programa_da_janela(hwnd):
    """O arquivo .exe do programa dono da janela (para abrir o CMS sozinho depois)."""
    import ctypes
    from ctypes import wintypes as W
    u, _ = _win()
    pid = W.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    k.CloseHandle.argtypes = [W.HANDLE]
    proc = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not proc:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
        return buf.value if k.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(n)) else ""
    finally:
        k.CloseHandle(proc)


class Tela:
    """A janela do CMS como se fosse a câmera: umas 5 imagens por segundo.
    Se o CMS sumir (fechado ou o computador reiniciou), abre ele de novo sozinho."""

    def __init__(self, titulo, por_segundo=5, programa=""):
        self.titulo = titulo.strip() or TITULO_CMS
        self.intervalo = 1.0 / por_segundo
        self.programa = programa or _CMS.get("programa", "")
        self.ultimo = 0.0
        self.erro = "" if windows() else "Ler a tela só funciona no Windows."
        self.morto = False
        self.falhas_max = 5  # uns 2 segundos sem a janela: avisa (minimizada, fechada...)
        if windows():
            preparar_tela()

    def isOpened(self):
        return windows()

    def get(self, _prop):
        return 0

    def read(self):
        espera = self.intervalo - (time.time() - self.ultimo)
        if espera > 0:
            time.sleep(espera)
        self.ultimo = time.time()
        img, self.erro = capturar_tela(self.titulo)
        if img is not None:
            _CMS["sumiu"] = 0.0
        elif self.erro.startswith("Não achei a janela"):
            self._abrir_cms()
        return img is not None, img

    def _abrir_cms(self):
        agora = time.time()
        _CMS["sumiu"] = _CMS["sumiu"] or agora
        if (not self.programa or not os.path.exists(self.programa) or agora - _CMS["sumiu"] < _CMS["esperar"]
                or agora - _CMS["abriu"] < _CMS["de_novo"]):
            return
        _CMS["abriu"] = agora
        log.info("O CMS of EseeCloud não está aberto. Abrindo ele...")
        try:
            subprocess.Popen([self.programa], cwd=os.path.dirname(self.programa))
        except OSError as e:
            log.info(f"Não consegui abrir o CMS: {e}")

    def release(self):
        pass


class Camera(threading.Thread):
    """Lê a câmera sem parar e guarda só a imagem mais nova (assim a leitura nunca fica atrasada)."""

    def __init__(self, url, estado):
        super().__init__(daemon=True)
        self.url, self.estado = url, estado
        self.trava = threading.Lock()
        self.quadro = None
        self.quando = 0.0
        self.parar = threading.Event()
        self.contagem, self.desde = 0, time.time()

    def ultimo(self):
        with self.trava:
            return (self.quadro, self.quando) if self.quadro is not None else (None, 0.0)

    def run(self):
        espera = 2
        # Vídeo gravado (para testar sem câmera): lê no ritmo do vídeo, como se fosse a câmera.
        arquivo = os.path.exists(self.url)
        while not self.parar.is_set():
            cap = abrir_video(self.url)
            ritmo = 1.0 / (cap.get(5) or 25) if arquivo else 0
            if not cap.isOpened():
                self._erro(getattr(cap, "erro", "") or "Não consegui abrir a câmera. Confira se ela está ligada e no cabo do roteador.")
                time.sleep(espera)
                espera = min(espera * 2, 60)
                continue
            espera = 2
            falhas = 0
            while not self.parar.is_set():
                ok, quadro = cap.read()
                if not ok or quadro is None:
                    falhas += 1
                    if falhas >= getattr(cap, "falhas_max", 25) or getattr(cap, "morto", False):
                        self._erro(getattr(cap, "erro", "") or "A câmera parou de mandar imagem.")
                        break
                    time.sleep(0.2)
                    continue
                falhas = 0
                if ritmo:
                    time.sleep(ritmo)
                with self.trava:
                    self.quadro, self.quando = quadro, time.time()
                self.contagem += 1
                if self.quando - self.desde >= 10:
                    self.estado["fps"] = round(self.contagem / (self.quando - self.desde), 1)
                    self.contagem, self.desde = 0, self.quando
                if not self.estado["camera_ok"]:
                    log.info("Câmera conectada.")
                    self.estado.update(camera_ok=True, camera_erro="")
            cap.release()
            time.sleep(2)

    def _erro(self, msg):
        if self.estado["camera_erro"] != msg:
            log.info(msg)
        self.estado.update(camera_ok=False, camera_erro=msg)


# ---------- Câmera ao vivo no painel ----------

def reduzir(img, largura=640):
    """A imagem com essa largura (e altura par, que o vídeo exige)."""
    import cv2
    h, w = img.shape[:2]
    if w <= largura and w % 2 == 0 and h % 2 == 0:
        return img
    nw = min(largura, w) // 2 * 2
    nh = max(2, int(round(h * nw / w / 2)) * 2)
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)


def foto_ao_vivo(img, maximo=90000, largura=640):
    """JPEG em base64 que cabe no cache da planilha (diminui a qualidade se precisar). HD: largura 1280 e até 185 KB."""
    import cv2
    img = reduzir(img, largura)
    for qualidade in (70, 55, 40, 30):
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualidade])
        if ok:
            b64 = base64.b64encode(jpg.tobytes()).decode("ascii")
            if len(b64) <= maximo:
                return b64
    return ""


def trilha_camera(pegar, fps=12, largura=640):
    """O vídeo que vai para o painel (WebRTC): a imagem mais nova da câmera, fps vezes por segundo."""
    import av
    import numpy as np
    from aiortc import VideoStreamTrack

    class Trilha(VideoStreamTrack):
        async def recv(self):
            lg = largura() if callable(largura) else largura
            if not hasattr(self, "_inicio"):
                self._inicio, self._pts = time.time(), 0
            else:
                self._pts += int(90000 / fps)
                espera = self._inicio + self._pts / 90000 - time.time()
                if espera > 0:
                    await asyncio.sleep(espera)
            img = pegar()
            img = reduzir(img, lg) if img is not None else np.zeros((360, 640, 3), np.uint8)
            quadro = av.VideoFrame.from_ndarray(img, format="bgr24")
            quadro.pts, quadro.time_base = self._pts, fractions.Fraction(1, 90000)
            return quadro
    return Trilha()


class AoVivo(threading.Thread):
    """Enquanto alguém olha a câmera no painel: responde o pedido de vídeo direto (WebRTC: o vídeo vai do computador para
    o celular sem passar pela planilha) e, para quem ainda não está no vídeo, manda uma imagem atrás da outra pela planilha.
    Sem ninguém olhando, só pergunta a cada 4 segundos (de dia e de noite: dá para olhar a oficina de casa)."""

    def __init__(self, cfg, pegar):
        """pegar(): (imagem mais nova da câmera, hora em que ela chegou)."""
        super().__init__(daemon=True)
        self.cfg, self.pegar = cfg, pegar
        self.parar = threading.Event()
        self.n = 0
        self.trava_n = threading.Lock()
        self.conexoes, self.ligadas = {}, set()
        self.loop = None
        self.sem_webrtc = False
        # Quem está olhando sem o vídeo direto precisa de imagens. A planilha demora ~1 segundo para responder cada uma:
        # um segundo envio, ao mesmo tempo, dobra as imagens por segundo.
        self.mandar_fotos = False
        # HD: alguém no painel ligou o botão HD (imagem de 1280 em vez de 640).
        self.hd = False
        threading.Thread(target=self._segundo_envio, daemon=True).start()

    def _com_foto(self, corpo):
        img, quando = self.pegar()
        foto = (foto_ao_vivo(img, 185000, 1280) if self.hd else foto_ao_vivo(img)) if img is not None else ""
        if foto:
            with self.trava_n:
                self.n += 1
                n = self.n
            corpo.update(foto=foto, fotoN=n, fotoHora=datetime.fromtimestamp(quando or time.time()).strftime("%H:%M:%S"),
                         fotoAtraso=round(max(0.0, time.time() - quando), 1) if quando else 0)
        return corpo

    def _segundo_envio(self):
        while not self.parar.is_set():
            if not self.mandar_fotos:
                self.parar.wait(0.3)
                continue
            try:
                r = enviar(self.cfg, self._com_foto({"acao": "camera_ao_vivo"}), tempo=20)
                self.mandar_fotos = bool(r.get("ok") and r.get("ver") and r.get("fotos", True))
                self.hd = bool(r.get("hd"))
            except Exception:
                self.parar.wait(3)

    def run(self):
        respostas, sessoes, ver, falhas = [], [], False, 0
        while not self.parar.is_set():
            corpo = {"acao": "camera_ao_vivo"}
            if self.mandar_fotos:
                self._com_foto(corpo)
            if respostas:
                corpo["respostas"] = respostas
            try:
                r = enviar(self.cfg, corpo, tempo=20)
                respostas, falhas = [], 0
            except Exception:
                falhas += 1
                self.parar.wait(min(60, 5 * falhas))
                continue
            if not r.get("ok"):
                self.mandar_fotos = False
                self.parar.wait(60)
                continue
            ver, sessoes = bool(r.get("ver")), list(r.get("sessoes") or [])
            # O painel é quem sabe se o vídeo direto está passando de verdade (às vezes conecta e trava).
            self.mandar_fotos = ver and bool(r.get("fotos", not (sessoes and all(s in self.ligadas for s in sessoes))))
            self.hd = bool(r.get("hd"))
            self._fechar_menos(sessoes)
            for o in r.get("ofertas") or []:
                sdp = self._responder(str(o.get("sessao", "")), str(o.get("sdp", "")))
                if sdp:
                    respostas.append({"sessao": o["sessao"], "sdp": sdp})
            if respostas:
                continue
            if ver:
                self.parar.wait(0.25)
            else:
                self.parar.wait(4)
        self.mandar_fotos = False
        self._fechar_menos([])

    def _no_loop(self, coro, tempo=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=tempo)

    def _responder(self, sessao, oferta):
        if self.loop is None:
            import importlib.util
            if importlib.util.find_spec("aiortc") is None:
                if not self.sem_webrtc:
                    log.info("Vídeo ao vivo direto: falta uma parte do leitor (aiortc). Rode o instalar.bat de novo. Por enquanto vão só imagens.")
                self.sem_webrtc = True
                return None
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, daemon=True).start()
        try:
            return self._no_loop(self._responder_async(sessao, oferta))
        except Exception as e:
            log.info(f"Vídeo ao vivo direto: não deu para responder o painel ({e}). Vão as imagens.")
            return None

    async def _responder_async(self, sessao, oferta):
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
        velha = self.conexoes.pop(sessao, None)
        if velha:
            await velha.close()
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=u) for u in (self.cfg.get("stun") or [])]))
        self.conexoes[sessao] = pc

        @pc.on("connectionstatechange")
        async def mudou():
            if pc.connectionState == "connected":
                self.ligadas.add(sessao)
                log.info("Vídeo ao vivo conectado direto com o painel.")
            elif pc.connectionState in ("failed", "closed"):
                self.ligadas.discard(sessao)
                if self.conexoes.get(sessao) is pc:
                    del self.conexoes[sessao]
                if pc.connectionState == "failed":
                    log.info("Vídeo ao vivo direto não conectou (a rede do celular não deixou). Vão as imagens.")
                    await pc.close()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=oferta, type="offer"))
        pc.addTrack(trilha_camera(lambda: self.pegar()[0], largura=lambda: 1280 if self.hd else 640))
        await pc.setLocalDescription(await pc.createAnswer())
        return pc.localDescription.sdp

    def _fechar_menos(self, sessoes):
        """Fecha o vídeo de quem não está mais olhando."""
        for s in [s for s in list(self.conexoes) if s not in sessoes]:
            pc = self.conexoes.pop(s, None)
            self.ligadas.discard(s)
            if pc is not None and self.loop is not None:
                try:
                    self._no_loop(pc.close(), 10)
                except Exception:
                    pass


# ---------- Leitura das placas ----------

def carregar_alpr(cfg):
    from fast_alpr import ALPR
    return ALPR(detector_model=cfg["detector"], ocr_model=cfg["ocr"])


MIN_RECORTE = 640  # o detector olha a imagem em 640x640: recorte menor que isso seria ampliado (placa grande demais)


def recortar(quadro, area):
    """Só a parte da imagem onde o carro passa (area em frações). Devolve (recorte, x0, y0).
    Se a área marcada for pequena, cresce em volta dela até 640x640 (nunca amplia a imagem)."""
    if not area or len(area) != 4:
        return quadro, 0, 0
    h, w = quadro.shape[:2]
    x1, y1, x2, y2 = (int(round(v * t)) for v, t in zip(area, (w, h, w, h)))
    x1, y1 = max(0, min(x1, w - 1)), max(0, min(y1, h - 1))
    x2, y2 = max(x1 + 1, min(x2, w)), max(y1 + 1, min(y2, h))

    def crescer(a, b, total):
        falta = min(MIN_RECORTE, total) - (b - a)
        if falta <= 0:
            return a, b
        a = max(0, a - falta // 2)
        b = min(total, a + min(MIN_RECORTE, total))
        return b - min(MIN_RECORTE, total), b
    x1, x2 = crescer(x1, x2, w)
    y1, y2 = crescer(y1, y2, h)
    return quadro[y1:y2, x1:x2], x1, y1


def confianca_ocr(ocr):
    c = getattr(ocr, "confidence", 0)
    if isinstance(c, (list, tuple)):
        return float(sum(c) / len(c)) if c else 0.0
    try:
        return float(c)
    except (TypeError, ValueError):
        try:
            return float(sum(c) / len(c))
        except Exception:
            return 0.0


def ler_placas(alpr, imagem, conf_minima=0.0):
    """[(placa, confiança, (x1, y1, x2, y2))] de uma imagem."""
    saida = []
    for r in alpr.predict(imagem):
        if not r.ocr or not r.ocr.text:
            continue
        placa = placas.normalizar(r.ocr.text)
        conf = confianca_ocr(r.ocr)
        if not placa or conf < conf_minima:
            continue
        b = r.detection.bounding_box
        saida.append((placa, conf, (int(b.x1), int(b.y1), int(b.x2), int(b.y2))))
    return saida


class Movimento:
    """Avisa quando algo mexe na imagem (não precisa ler placa com o portão parado)."""

    def __init__(self):
        self.fundo = None

    def mexeu(self, imagem):
        import cv2
        peq = cv2.resize(imagem, (320, max(1, int(320 * imagem.shape[0] / imagem.shape[1]))))
        cinza = cv2.GaussianBlur(cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY), (7, 7), 0).astype("float32")
        if self.fundo is None or self.fundo.shape != cinza.shape:
            self.fundo = cinza
            return True
        dif = cv2.absdiff(cinza, self.fundo)
        cv2.accumulateWeighted(cinza, self.fundo, 0.08)
        return float((dif > 18).mean()) > 0.004


def guardar_foto(quadro, placa, quando, caixa=None):
    import cv2
    pasta = PASTA_FOTOS / quando.strftime("%Y-%m-%d")
    pasta.mkdir(parents=True, exist_ok=True)
    img = quadro.copy()
    if caixa:
        cv2.rectangle(img, caixa[:2], caixa[2:], (0, 200, 0), 3)
        cv2.putText(img, placa, (caixa[0], max(30, caixa[1] - 12)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 200, 0), 3)
    if img.shape[1] > 1280:
        img = cv2.resize(img, (1280, int(1280 * img.shape[0] / img.shape[1])))
    arq = pasta / f"{quando.strftime('%H%M%S')}_{placa}.jpg"
    cv2.imwrite(str(arq), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return arq


def apagar_fotos_antigas(dias):
    if not PASTA_FOTOS.exists():
        return
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    for p in PASTA_FOTOS.iterdir():
        if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", p.name) and p.name < limite:
            shutil.rmtree(p, ignore_errors=True)


def recarregar(cfg, visto):
    """O config-blincar.json mudou (marcar-area.bat, usar-cms.bat ou procurar-camera.bat com o leitor ligado)?
    Pega a área e a câmera novas. Devolve o que mudou."""
    try:
        m = ARQ_CONFIG.stat().st_mtime
    except OSError:
        return set()
    if m == visto.get("mtime"):
        return set()
    primeira = "mtime" not in visto
    visto["mtime"] = m
    try:
        novo = json.loads(ARQ_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return set()
    mudou = {k for k in ("area", "camera", "cms_programa", "contar", "ponto", "placa_na_mao") if k in novo and novo[k] != cfg.get(k)}
    for k in mudou:
        cfg[k] = novo[k]
    return set() if primeira else mudou


def na_mao(caixa, tamanho, cfg):
    """A placa foi mostrada na mão? Grande na imagem (perto da câmera) e no lugar marcado (ponto).
    Devolve (sim, motivo): motivo "pequena" quando está no ponto mas longe demais."""
    h, w = tamanho
    x1, y1, x2, y2 = caixa
    ponto = cfg.get("ponto") or []
    if len(ponto) == 4:
        cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
        if not (ponto[0] <= cx <= ponto[2] and ponto[1] <= cy <= ponto[3]):
            return False, "fora"
    if (x2 - x1) / w < float(cfg.get("placa_na_mao", 0.05)):
        return False, "pequena"
    return True, ""


def apitar():
    """Dois bipes no computador: a placa mostrada na mão foi lida."""
    if os.name != "nt":
        return

    def tocar():
        try:
            import winsound
            winsound.Beep(1500, 130)
            winsound.Beep(2000, 200)
        except Exception:
            try:
                import winsound
                winsound.MessageBeep()
            except Exception:
                pass
    threading.Thread(target=tocar, daemon=True).start()


def rodar(cfg):
    falta = faltando(cfg)
    if falta:
        raise SystemExit("Falta " + " e ".join(falta) + ". No painel: aba Câmera > \"Ligar a câmera\" baixa o config-blincar.json. "
                         "Coloque ele nesta pasta (ou deixe na pasta Downloads).")
    if not cfg.get("camera"):
        raise SystemExit("Ainda não sei o endereço da câmera. Rode o procurar-camera.bat primeiro "
                         "(ou o usar-cms.bat, para ler as placas na janela do CMS of EseeCloud).")
    _CMS["programa"] = cfg.get("cms_programa", "")
    try:
        trava = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trava.bind(("127.0.0.1", PORTA_TRAVA))
    except OSError:
        raise SystemExit("O leitor já está ligado neste computador.")

    log.info(f"Leitor de placas Blin Car {VERSAO} ligando...")
    estado = {"camera_ok": False, "camera_erro": "Conectando na câmera..."}
    envio = Envio(cfg, estado)
    envio.start()
    cam = Camera(cfg["camera"], estado)
    cam.start()
    atual = {"cam": cam}
    # O ao vivo já funciona enquanto a leitura de placas carrega (leva uns segundos).
    aovivo = AoVivo(cfg, lambda: atual["cam"].ultimo()) if cfg.get("ao_vivo", True) else None
    if aovivo:
        aovivo.start()
    alpr = carregar_alpr(cfg)
    mov = Movimento()
    passagens = placas.Passagens(pausa=cfg["segundos_fim_passagem"], min_leituras=cfg["min_leituras"],
                                 so_andando=bool(cfg.get("so_carro_andando", True)))
    ja_mandadas, avisadas_paradas = {}, {}
    # Modo "mao": a placa segurada um instante, sem precisar andar. Conta (e apita) na 3ª leitura, com a placa ainda
    # na mão. A mesma placa vista no ponto nos 10 minutos antes não conta de novo (placa largada ali, carro parado).
    maos = placas.Passagens(pausa=float(cfg.get("segundos_mao", 2)), min_leituras=2, conf_sozinha=0.97, duracao_max=20)
    avisos_pequena, vistas_mao = {}, {}
    intervalo = 1.0 / max(0.5, float(cfg["analises_por_segundo"]))
    ultimo_mov = 0.0
    ultima_limpeza = 0.0
    ultimo_quadro = 0.0
    if cfg.get("contar", "mao") == "mao":
        log.info("Pronto. Mostrem a placa na mão para a câmera" + (" (no lugar marcado)" if cfg.get("ponto") else "") +
                 ": o computador apita quando ler. Feche esta janela para desligar.")
    else:
        log.info("Pronto. Olhando o portão (feche esta janela para desligar).")
    visto, ultima_olhada, mao_prontas = {}, 0.0, []
    recarregar(cfg, visto)
    try:
        while True:
            inicio = time.time()
            if inicio - ultima_limpeza > 3600:
                ultima_limpeza = inicio
                apagar_fotos_antigas(cfg["dias_guardar_fotos"])
            if inicio - ultima_olhada > 5:
                ultima_olhada = inicio
                mudou = recarregar(cfg, visto)
                if "area" in mudou:
                    log.info("Peguei a área nova marcada." if cfg["area"] else "Agora olhando a imagem toda.")
                _CMS["programa"] = cfg.get("cms_programa", "")
                if "camera" in mudou and cfg.get("camera"):
                    log.info("A câmera mudou na configuração. Conectando na nova...")
                    cam.parar.set()
                    cam = Camera(cfg["camera"], estado)
                    cam.start()
                    atual["cam"] = cam
            quadro, quando = cam.ultimo()
            if quadro is not None and quando != ultimo_quadro:
                ultimo_quadro = quando
                modo_mao = cfg.get("contar", "mao") == "mao"
                # Modo "mao": olha só o ponto onde mostram a placa; modo "carros": a área por onde os carros passam.
                parte, x0, y0 = recortar(quadro, (cfg.get("ponto") or []) if modo_mao else cfg["area"])
                if mov.mexeu(parte):
                    ultimo_mov = inicio
                # Lê enquanto algo se mexe e mais 3 segundos depois.
                if inicio - ultimo_mov <= 3:
                    for placa, conf, (x1, y1, x2, y2) in ler_placas(alpr, parte, cfg["confianca_minima"]):
                        caixa = (x1 + x0, y1 + y0, x2 + x0, y2 + y0)
                        if not modo_mao:
                            passagens.adicionar(placa, conf, x2 - x1, inicio, (quadro, caixa, datetime.now()), caixa)
                            continue
                        sim, motivo = na_mao(caixa, quadro.shape[:2], cfg)
                        if sim:
                            alvo = maos.adicionar(placa, conf, x2 - x1, inicio, (quadro, caixa, datetime.now()), caixa)
                            if alvo is not None:
                                chave_m = placas.mercosul(alvo["leituras"][0][0])
                                if "antes" not in alvo:
                                    alvo["antes"] = vistas_mao.get(chave_m, 0)
                                vistas_mao[chave_m] = inicio
                                if not alvo.get("contou") and len(alvo["leituras"]) >= 3:
                                    alvo["contou"] = True
                                    placa_v, conf_v = placas.votar(alvo["leituras"])
                                    if inicio - alvo["antes"] > 600:
                                        mao_prontas.append({"placa": placa_v, "confianca": round(conf_v, 3), "leituras": len(alvo["leituras"]),
                                                            "direcao": "", "inicio": alvo["inicio"], "fim": inicio, "melhor": alvo["melhor"], "mao": True})
                                    else:
                                        log.info(f"Placa {placa_v} mostrada de novo: ela já foi vista aqui há pouco (menos de 10 minutos), não conta outra vez.")
                        elif motivo == "pequena" and inicio - avisos_pequena.get(placa, 0) > 60:
                            avisos_pequena[placa] = inicio
                            log.info(f"Placa {placa} vista, mas pequena ({round(100 * (x2 - x1) / quadro.shape[1])}% da imagem): "
                                     "para contar, mostre a placa mais perto da câmera.")
            terminadas = passagens.terminadas(time.time()) + mao_prontas
            mao_prontas = []
            maos.terminadas(time.time())  # só fecha as passagens: a da mão já contou na 3ª leitura
            for placa in passagens.placas_paradas:
                # Carro estacionado na frente da câmera: anota uma vez a cada 6 horas e não manda.
                if time.time() - avisadas_paradas.get(placa, 0) > 6 * 3600:
                    avisadas_paradas[placa] = time.time()
                    log.info(f"Placa {placa} parada (carro estacionado): não conta. Só conta carro entrando ou saindo.")
            passagens.placas_paradas.clear()
            for p in terminadas:
                chave = placas.mercosul(p["placa"])
                if time.time() - ja_mandadas.get(chave, 0) < cfg["minutos_mesma_placa"] * 60:
                    continue
                ja_mandadas[chave] = time.time()
                quadro_m, caixa_m, quando_m = p["melhor"]
                quando_txt = datetime.fromtimestamp(p["inicio"]).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    foto = guardar_foto(quadro_m, p["placa"], quando_m, caixa_m)
                except Exception as e:
                    foto = f"(não guardei a foto: {e})"
                if p.get("mao"):
                    apitar()
                    log.info(f"Placa {p['placa']} mostrada na mão  ({p['leituras']} leituras, {round(p['confianca'] * 100)}%)  foto: {foto}")
                else:
                    log.info(f"Placa {p['placa']}  ({p['leituras']} leituras, {round(p['confianca'] * 100)}%, {p['direcao'] or 'sem direção'})  foto: {foto}")
                envio.mandar({"acao": "camera_evento", "placa": p["placa"], "confianca": p["confianca"], "leituras": p["leituras"],
                              "direcao": "" if p.get("mao") else p["direcao"], "quando": quando_txt, "mao": bool(p.get("mao"))})
            time.sleep(max(0.0, intervalo - (time.time() - inicio)))
    except KeyboardInterrupt:
        log.info("Leitor desligado.")
    finally:
        cam.parar.set()
        envio.parar.set()
        if aovivo:
            aovivo.parar.set()


# ---------- Testar uma foto ou vídeo ----------

def testar(cfg, arquivo):
    import cv2
    arq = Path(arquivo)
    if not arq.exists():
        raise SystemExit(f"Não achei o arquivo {arq}.")
    print("Carregando a leitura de placas...")
    alpr = carregar_alpr(cfg)
    saida = PASTA / "teste-resultado.jpg"
    if arq.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        img = cv2.imread(str(arq))
        if img is None:
            raise SystemExit("Não consegui abrir essa imagem.")
        achadas = ler_placas(alpr, img)
        for placa, conf, caixa in achadas:
            print(f"  {placa}  ({round(conf * 100)}%)  largura da placa: {caixa[2] - caixa[0]} pixels")
            cv2.rectangle(img, caixa[:2], caixa[2:], (0, 200, 0), 3)
            cv2.putText(img, placa, (caixa[0], max(30, caixa[1] - 12)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 200, 0), 3)
        if not achadas:
            print("  Nenhuma placa lida nessa imagem.")
        cv2.imwrite(str(saida), img)
        print(f"Imagem marcada: {saida}")
        return achadas
    cap = cv2.VideoCapture(str(arq))
    passagens = placas.Passagens(pausa=cfg["segundos_fim_passagem"], min_leituras=cfg["min_leituras"],
                                 so_andando=bool(cfg.get("so_carro_andando", True)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    pular = max(1, int(round(fps / max(0.5, float(cfg["analises_por_segundo"])))))
    n, resultado, melhor = 0, [], None
    while True:
        ok, quadro = cap.read()
        if not ok:
            break
        n += 1
        if n % pular:
            continue
        t = n / fps
        parte, x0, y0 = recortar(quadro, cfg["area"])
        for placa, conf, (x1, y1, x2, y2) in ler_placas(alpr, parte, cfg["confianca_minima"]):
            passagens.adicionar(placa, conf, x2 - x1, t, None, (x1, y1, x2, y2))
            if melhor is None or conf > melhor[1]:
                melhor = (quadro.copy(), conf, placa, (x1 + x0, y1 + y0, x2 + x0, y2 + y0))
        resultado += passagens.terminadas(t)
    resultado += passagens.terminadas(n / fps + 999, forcar=True)
    for p in resultado:
        print(f"  {p['placa']}  ({p['leituras']} leituras, {round(p['confianca'] * 100)}%, {p['direcao'] or 'sem direção'}) aos {int(p['inicio'])}s")
    if not resultado:
        print("  Nenhuma placa lida nesse vídeo.")
    if passagens.paradas:
        print(f"  ({passagens.paradas} vez(es) uma placa parada: carro estacionado não conta)")
    if melhor:
        img, _, placa, caixa = melhor
        cv2.rectangle(img, caixa[:2], caixa[2:], (0, 200, 0), 3)
        cv2.putText(img, placa, (caixa[0], max(30, caixa[1] - 12)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 200, 0), 3)
        cv2.imwrite(str(saida), img)
        print(f"Melhor imagem marcada: {saida}")
    return resultado


# ---------- Procurar a câmera na rede ----------

# "/ch0_0.264": câmeras da Juan (app IP Pro / dvr163), que costumam mandar o vídeo pela própria porta 80.
CAMINHOS = ["/ch0_0.264", "/ch0_1.264", "/live/ch00_0", "/live/ch00_1", "/onvif1", "/onvif2", "/11", "/12", "/stream1",
            "/h264/ch1/main/av_stream", "/cam/realmonitor?channel=1&subtype=0", "/live", "/ch0_0.h264", "/av0_0", "/"]
# Caminhos usados só para ver se uma porta responde como vídeo.
CAMINHOS_TESTE = ["/", "/ch0_0.264", "/live/ch00_0"]
# Sem senha e o usuário "admin" sem senha. Se a câmera pedir senha, o procurar-camera.bat pergunta a dela.
USUARIOS = [("", ""), ("admin", "")]
# Portas onde as câmeras costumam mandar o vídeo (RTSP) e outras que ajudam a reconhecer o aparelho.
PORTAS_RTSP = [554, 8554, 10554, 5554]
PORTAS_OUTRAS = [80, 81, 443, 8000, 8080, 8800, 8899, 34567, 5000, 9527, 23, 10000]


def meu_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def porta_aberta(ip, porta=554, tempo=0.6):
    try:
        with socket.create_connection((ip, porta), timeout=tempo):
            return True
    except OSError:
        return False


def responde_ping(ip):
    """O aparelho responde ao ping? (procura "TTL=" na resposta, que só aparece quando ele responde de verdade)."""
    import subprocess
    if os.name == "nt":
        cmd, extra = ["ping", "-n", "1", "-w", "700", ip], {"creationflags": 0x08000000}
    else:
        cmd, extra = ["ping", "-c", "1", "-W", "1", ip], {}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=4, **extra)
    except Exception:
        return False
    return "ttl=" in (r.stdout or "").lower()


def resposta_rtsp(ip, caminho, porta=554, tempo=2.5):
    """Código da resposta RTSP (200 abre, 401 pede senha, 404 caminho errado; 0 = não respondeu)."""
    try:
        with socket.create_connection((ip, porta), timeout=tempo) as s:
            s.settimeout(tempo)
            url = f"rtsp://{ip}:{porta}{caminho}"
            s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\nUser-Agent: LeitorBlinCar\r\n\r\n".encode())
            dados = s.recv(2048).decode("latin-1", errors="replace")
    except OSError:
        return 0
    m = re.match(r"RTSP/1\.\d (\d{3})", dados)
    return int(m.group(1)) if m else 0


def com_senha(ip, caminho, usuario, senha, porta=554):
    if not usuario:
        return f"rtsp://{ip}:{porta}{caminho}"
    return f"rtsp://{urllib.parse.quote(usuario, safe='')}:{urllib.parse.quote(senha, safe='')}@{ip}:{porta}{caminho}"


def imagem_e_erro(url, tentativas=10):
    """(imagem, motivo) de uma imagem da câmera."""
    cap = abrir_video(url, 5000)
    try:
        if not cap.isOpened():
            return None, getattr(cap, "erro", "") or "A câmera não abriu."
        for _ in range(tentativas):
            ok, q = cap.read()
            if ok and q is not None:
                return q, ""
            if getattr(cap, "morto", False):
                break
        return None, getattr(cap, "erro", "") or "A câmera não mandou imagem."
    finally:
        cap.release()


def pega_imagem(url):
    return imagem_e_erro(url)[0]


def varrer(base, meu, portas):
    """{ip: [portas abertas]} de todos os aparelhos da rede (base.1 a base.254) com alguma dessas portas."""
    pares = [(f"{base}.{i}", p) for i in range(1, 255) if f"{base}.{i}" != meu for p in portas]
    achados = {}
    with concurrent.futures.ThreadPoolExecutor(128) as ex:
        for (ip, p), ok in zip(pares, ex.map(lambda par: porta_aberta(*par), pares)):
            if ok:
                achados.setdefault(ip, []).append(p)
    return achados


def aparelhos(base, meu):
    """{ip: [portas abertas]} de todo aparelho que responde (ping ou alguma porta conhecida)."""
    achados = varrer(base, meu, PORTAS_RTSP + PORTAS_OUTRAS)
    ips = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != meu]
    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        for ip, ok in zip(ips, ex.map(responde_ping, ips)):
            if ok:
                achados.setdefault(ip, [])
    return achados


def portas_todas(ip):
    """Todas as portas abertas do aparelho (1 a 10000 e algumas altas comuns em câmera)."""
    portas = list(range(1, 10001)) + [10554, 15554, 20000, 34567, 34599, 37777, 49152, 50000]
    abertas = []
    with concurrent.futures.ThreadPoolExecutor(256) as ex:
        for p, ok in zip(portas, ex.map(lambda porta: porta_aberta(ip, porta, 0.5), portas)):
            if ok:
                abertas.append(p)
    return abertas


def portas_de_video(ip, completo=False):
    """(portas que respondem como vídeo de câmera, portas abertas). "completo": olha todas as portas do aparelho."""
    portas = portas_todas(ip) if completo else [p for p in sorted(set(PORTAS_RTSP + PORTAS_OUTRAS)) if porta_aberta(ip, p, 1.5)]
    return [p for p in portas if any(resposta_rtsp(ip, c, p, 1.5) > 0 for c in CAMINHOS_TESTE)], portas


def achou_camera(cfg, url, ip, img):
    import cv2
    if cfg.get("camera") != url:
        cfg["area"] = []  # imagem nova: a área marcada antes não vale mais
    cfg["camera"] = url
    salvar_config(cfg)
    foto = PASTA / "foto-camera.jpg"
    cv2.imwrite(str(foto), img)
    print(f"\nACHEI A CÂMERA: {ip} (imagem de {img.shape[1]}x{img.shape[0]}).")
    print(f"Guardei o endereço no {ARQ_CONFIG.name} e tirei uma foto: {foto}")
    if cfg.get("contar", "mao") == "mao":
        if not cfg.get("ponto"):
            print("Agora abra o marcar-ponto.bat e marque o lugar onde vocês vão mostrar a placa na mão.")
    elif not cfg.get("area"):
        print("Agora abra o marcar-area.bat e mostre com o mouse onde os carros passam.")


def tentar_bubble(cfg, ip, porta, usuarios, relatorio):
    """Vídeo direto das câmeras da EseeCloud. Devolve (url, pediu_senha)."""
    pares = []
    for u, s in usuarios:
        if (u or "admin", s) not in pares:
            pares.append((u or "admin", s))
    pediu = False
    for usuario, senha in pares:
        url = (f"bubble://{urllib.parse.quote(usuario, safe='')}:{urllib.parse.quote(senha, safe='')}@{ip}:{porta}"
               "/bubble/live?ch=0&stream=0")
        img, erro = imagem_e_erro(url, 60)
        if img is not None:
            achou_camera(cfg, url, ip, img)
            return url, False
        if "não aceitou a senha" in erro:
            pediu = True
            anotar(f"  {ip}:{porta}: não abriu com a senha {'(vazia)' if not senha else 'digitada'}. "
                   f"Resposta da câmera: {RESPOSTAS_LOGIN.get(f'{ip}:{porta}', '?')}")
            continue
        relatorio.append(f"  {ip}:{porta} (vídeo da EseeCloud): {erro}")
        break
    return None, pediu


def tentar_camera(cfg, ip, porta, usuarios):
    """Tenta abrir o vídeo nessa porta. Devolve (url, pediu_senha)."""
    codigos = {c: resposta_rtsp(ip, c, porta) for c in CAMINHOS}
    abertos = [c for c in CAMINHOS if codigos[c] == 200]
    pede = [c for c in CAMINHOS if codigos[c] == 401]
    tentativas = [(c, "", "") for c in abertos] + [(c, u, s) for u, s in usuarios if u for c in pede]
    for caminho, usuario, senha in tentativas:
        url = com_senha(ip, caminho, usuario, senha, porta)
        img = pega_imagem(url)
        if img is None:
            continue
        achou_camera(cfg, url, ip, img)
        return url, False
    return None, bool(pede)


def anotar(texto):
    print(texto)
    with open(PASTA / "procurar.log", "a", encoding="utf-8") as f:
        f.write(texto + "\n")


def ordenar_ips(ips):
    return sorted(set(ips), key=lambda x: [int(n) for n in x.split(".")])


def descobrir_camera(base, meu, antes):
    """A câmera não respondeu como câmera: acha qual aparelho é ela, tirando da tomada e vendo quem some."""
    input("\nAgora TIRE A CÂMERA DA TOMADA, espere uns 20 segundos e aperte Enter...")
    depois = aparelhos(base, meu)
    sumiram = ordenar_ips(set(antes) - set(depois))
    input("Pode LIGAR A CÂMERA de novo. Espere 1 minuto (ela demora para ligar) e aperte Enter...")
    if not sumiram:
        anotar("Nenhum aparelho sumiu quando a câmera foi desligada: ela não está na mesma rede deste computador.\n"
               "  - Confira se o computador está na internet do MESMO roteador em que a câmera está no cabo\n"
               "    (se tiver repetidor ou duas redes, ligue o computador direto na rede do roteador);\n"
               "  - a câmera pode ter guardado o endereço da oficina antiga: segure o botão de reset dela por 10 segundos,\n"
               "    espere 1 minuto e rode o procurar-camera.bat de novo.")
        return []
    # Pode sumir mais de um: a câmera no cabo e no Wi-Fi ao mesmo tempo (dois endereços), ou um celular que saiu da rede
    # na mesma hora. Todos ficam guardados e são testados; da próxima vez não precisa tirar da tomada.
    anotar(f"Sumiram quando a câmera foi desligada: {', '.join(sumiram)}.")
    return sumiram


def testar_aparelhos(cfg, ips, usuarios, completo, relatorio):
    """Tenta abrir o vídeo em cada aparelho. Devolve (url, aparelhos que pediram senha)."""
    pede = []
    for ip in ips:
        if completo:
            print(f"Olhando todas as portas de {ip} (uns 30 segundos)...")
        video, portas = portas_de_video(ip, completo)
        # Câmeras da EseeCloud (CMS of EseeCloud / IP Pro): o vídeo no formato delas, em qualquer porta aberta.
        bubble = [p for p in portas if p not in video and resposta_bubble(ip, p)]
        for porta in video + bubble:
            print(f"A porta {porta} de {ip} responde como vídeo{' (EseeCloud)' if porta in bubble else ''}. Testando...")
            if porta in bubble:
                url, pediu = tentar_bubble(cfg, ip, porta, usuarios, relatorio)
            else:
                url, pediu = tentar_camera(cfg, ip, porta, usuarios)
            if url:
                cfg["camera_ip"] = ip
                salvar_config(cfg)
                return url, pede
            if pediu and ip not in pede:
                pede.append(ip)
        if completo:
            relatorio.append(f"  {ip}: portas abertas {', '.join(map(str, portas)) or 'nenhuma'}; "
                             f"respondem como vídeo: {', '.join(map(str, video + bubble)) or 'nenhuma'}")
    return None, pede


PLANO_B = ("Se o vídeo da câmera aparece no CMS of EseeCloud, use o usar-cms.bat: "
           "o leitor lê as placas na janela do CMS (deixe o CMS aberto com a câmera).")


def procurar(cfg, ip_camera="", senha_extra=""):
    print("Procurando a câmera na rede (pode levar 1 ou 2 minutos)...")
    meu = meu_ip()
    if not meu and not ip_camera:
        raise SystemExit("Este computador não está em nenhuma rede. Conecte no Wi-Fi ou no cabo do roteador da oficina.")
    base = ".".join(meu.split(".")[:3])
    usuarios = list(USUARIOS)
    if senha_extra:
        usuarios = [("admin", senha_extra)] + usuarios
    anotar(f"\n--- {datetime.now():%d/%m %H:%M} Procurando a câmera (este computador: {meu or '?'}) ---")
    relatorio, pede = [], []

    def pediu_senha(ips):
        cfg["camera_ip"] = ips[0]
        salvar_config(cfg)
        # Código 3: o procurar-camera.bat pergunta a senha e tenta de novo.
        print(f"A câmera em {', '.join(ordenar_ips(ips))} pede senha." + (" Essa senha não abriu." if senha_extra else "") +
              " É a mesma senha que vocês usam no CMS of EseeCloud (ou no aplicativo da câmera).")
        raise SystemExit(3)

    if ip_camera:
        url, pede = testar_aparelhos(cfg, [ip_camera], usuarios, True, relatorio)
        if url:
            return url
        if pede:
            pediu_senha(pede)
        anotar("\n".join(relatorio))
        raise SystemExit(f"Não consegui abrir o vídeo de {ip_camera}.")

    # 1) Aparelhos com a porta de vídeo comum.
    print(f"Olhando os aparelhos de {base}.1 a {base}.254...")
    candidatos = varrer(base, meu, PORTAS_RTSP)
    url, p = testar_aparelhos(cfg, list(candidatos), usuarios, False, relatorio)
    if url:
        return url
    pede += p
    # 2) Todos os aparelhos da rede.
    atuais = aparelhos(base, meu)
    anotar(f"Aparelhos na rede agora ({len(atuais)}):")
    for ip in ordenar_ips(atuais):
        anotar(f"  {ip}   portas: {', '.join(map(str, sorted(atuais[ip]))) or 'nenhuma das conhecidas'}")
    # 3) Os aparelhos da câmera guardados de antes (o que sumiu quando ela foi tirada da tomada): todas as portas.
    guardados = [ip for ip in (cfg.get("camera_ips") or []) + [cfg.get("camera_ip") or ""] if ip and ip.startswith(base + ".")]
    vivos = [ip for ip in ordenar_ips(guardados) if ip in atuais]
    if vivos:
        anotar(f"Aparelhos da câmera guardados: {', '.join(vivos)}. Não precisa tirar da tomada.")
        url, p = testar_aparelhos(cfg, vivos, usuarios, True, relatorio)
        if url:
            return url
        pede += p
    # 4) Os outros aparelhos da rede, nas portas conhecidas.
    outros = [ip for ip in ordenar_ips(atuais) if ip not in vivos and ip not in candidatos]
    url, p = testar_aparelhos(cfg, outros, usuarios, False, relatorio)
    if url:
        return url
    pede += p
    if pede:
        pediu_senha(pede)
    if vivos:
        anotar("A câmera está na rede, mas nenhuma porta dela mandou o vídeo para o computador:\n" + "\n".join(relatorio) +
               "\n" + PLANO_B + "\nSe não der, tire um print desta janela (ou mande o arquivo procurar.log) para o Arthur.")
        raise SystemExit(1)
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("Confira se a câmera está ligada (fonte 12V) e com o cabo no roteador, e se este computador está na "
                         "internet do mesmo roteador. Depois rode o procurar-camera.bat de novo.")
    # 5) Ainda não sabe qual aparelho é a câmera: tira da tomada uma vez e guarda quem sumiu.
    print("Não achei a câmera do jeito comum. Vamos descobrir qual aparelho é ela (só desta vez).")
    sumiram = descobrir_camera(base, meu, atuais)
    if not sumiram:
        raise SystemExit(1)
    cfg["camera_ips"] = sumiram
    salvar_config(cfg)
    url, p = testar_aparelhos(cfg, sumiram, usuarios, True, relatorio)
    if url:
        return url
    if p:
        pediu_senha(p)
    anotar("A câmera está na rede, mas nenhuma porta dela mandou o vídeo para o computador:\n" + "\n".join(relatorio) +
           "\nGuardei esses aparelhos: da próxima vez não precisa tirar da tomada.\n" + PLANO_B +
           "\nSe não der, tire um print desta janela (ou mande o arquivo procurar.log) para o Arthur.")
    raise SystemExit(1)


# ---------- Usar a janela do CMS e marcar a área dos carros ----------

def usar_tela(cfg, titulo=""):
    """Passa a ler as placas na janela do CMS of EseeCloud (a câmera aparecendo nele)."""
    import cv2
    titulo = (titulo or TITULO_CMS).strip()
    if not windows():
        raise SystemExit("Ler a tela só funciona no Windows.")
    preparar_tela(acordado=False)
    img, erro = capturar_tela(titulo)
    if img is None:
        raise SystemExit(erro)
    url = "tela:" + titulo
    if cfg.get("camera") != url:
        cfg["area"] = []  # imagem nova: a área marcada antes não vale mais
    cfg["camera"] = url
    janelas = achar_janelas(titulo)
    programa = programa_da_janela(janelas[0][0]) if janelas else ""
    if programa:
        cfg["cms_programa"] = programa  # para abrir o CMS sozinho se ele for fechado
    salvar_config(cfg)
    foto = PASTA / "foto-camera.jpg"
    cv2.imwrite(str(foto), img)
    print(f'Pronto! O leitor vai ler as placas na janela "{titulo}" ({img.shape[1]}x{img.shape[0]}).')
    print(f"Tirei uma foto do que ele está vendo: {foto}")
    print("Deixe o CMS aberto com o vídeo da câmera (pode ficar atrás de outras janelas, só não minimize).")
    print("Agora abra o " + ("marcar-ponto.bat e marque onde vocês vão mostrar a placa na mão."
                             if cfg.get("contar", "mao") == "mao" else "marcar-area.bat e mostre com o mouse onde os carros passam."))
    return url


def area_do_retangulo(x1, y1, x2, y2, largura, altura):
    """Retângulo em pixels da imagem mostrada para frações [esquerda, topo, direita, baixo]."""
    x1, x2 = sorted((max(0, min(x1, largura)), max(0, min(x2, largura))))
    y1, y2 = sorted((max(0, min(y1, altura)), max(0, min(y2, altura))))
    return [round(x1 / largura, 4), round(y1 / altura, 4), round(x2 / largura, 4), round(y2 / altura, 4)]


def desenhar_area(img, area):
    """A imagem com o resto escurecido e um retângulo verde em volta da área."""
    import cv2
    img = img.copy()
    if area and len(area) == 4:
        h, w = img.shape[:2]
        x1, y1, x2, y2 = (int(round(v * t)) for v, t in zip(area, (w, h, w, h)))
        escuro = (img * 0.4).astype(img.dtype)
        escuro[y1:y2, x1:x2] = img[y1:y2, x1:x2]
        img = escuro
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), max(2, w // 400))
    return img


TEXTOS_MARCAR = {
    "area": {"titulo": "Marcar onde os carros passam - Blin Car", "toda": "Usar a imagem toda",
             "explica": "Arraste o mouse fazendo um retângulo em volta do lugar por onde os carros passam, com folga "
                        "(a placa tem que caber inteira, do carro chegando até ele parar). Depois clique em Salvar.",
             "salvo": "O leitor só vai olhar essa parte da imagem.", "tudo": "Pronto: o leitor vai olhar a imagem toda."},
    "ponto": {"titulo": "Marcar onde vocês mostram a placa - Blin Car", "toda": "Qualquer lugar",
              "explica": "Arraste o mouse fazendo um retângulo em volta do lugar onde vocês vão segurar a placa para a câmera "
                         "(perto dela, onde não para carro). Com folga: a placa tem que caber inteira. Depois clique em Salvar.",
              "salvo": "Só conta a placa mostrada nesse lugar.", "tudo": "Pronto: a placa mostrada em qualquer lugar conta (se estiver perto da câmera)."},
}


def marcar_area(cfg, chave="area"):
    """Mostra a imagem da câmera para marcar com o mouse um retângulo: onde os carros passam ("area")
    ou onde mostram a placa na mão ("ponto")."""
    t = TEXTOS_MARCAR[chave]
    import base64
    import cv2
    if not cfg.get("camera"):
        raise SystemExit("Ainda não sei de onde vem a imagem da câmera. Rode o procurar-camera.bat (ou o usar-cms.bat) primeiro.")
    print("Pegando uma imagem da câmera...")
    preparar_tela(acordado=False)
    img, erro = imagem_e_erro(cfg["camera"], 60)
    if img is None:
        raise SystemExit("Não consegui pegar a imagem da câmera. " + erro)
    import tkinter as tk
    h, w = img.shape[:2]
    raiz = tk.Tk()
    raiz.title(t["titulo"])
    escala = min(1.0, (raiz.winfo_screenwidth() - 60) / w, (raiz.winfo_screenheight() - 240) / h)
    pw, ph = max(1, int(w * escala)), max(1, int(h * escala))
    mostrar = cv2.resize(img, (pw, ph), interpolation=cv2.INTER_AREA) if escala < 1 else img
    foto = tk.PhotoImage(data=base64.b64encode(cv2.imencode(".png", mostrar)[1].tobytes()).decode("ascii"))
    tk.Label(raiz, font=("Segoe UI", 11), justify="left", wraplength=max(420, pw), text=t["explica"]).pack(padx=10, pady=(8, 4), anchor="w")
    tela = tk.Canvas(raiz, width=pw, height=ph, cursor="crosshair", highlightthickness=0)
    tela.pack(padx=10)
    tela.create_image(0, 0, image=foto, anchor="nw")
    barra = tk.Frame(raiz)
    barra.pack(fill="x", padx=10, pady=8)
    aviso = tk.Label(barra, text="", fg="#b00000", font=("Segoe UI", 10))
    aviso.pack(side="left")
    estado = {"inicio": None, "area": list(cfg.get(chave) or []), "resultado": None}

    def desenhar(area):
        tela.delete("ret")
        if area:
            tela.create_rectangle(area[0] * pw, area[1] * ph, area[2] * pw, area[3] * ph, outline="#00e000", width=3, tags="ret")

    def apertou(e):
        estado["inicio"] = (e.x, e.y)

    def arrastou(e):
        if estado["inicio"]:
            desenhar(area_do_retangulo(*estado["inicio"], e.x, e.y, pw, ph))

    def soltou(e):
        if not estado["inicio"]:
            return
        area = area_do_retangulo(*estado["inicio"], e.x, e.y, pw, ph)
        estado["inicio"] = None
        if area[2] - area[0] < 0.03 or area[3] - area[1] < 0.03:
            aviso.config(text="Ficou pequeno demais. Arraste de novo, com mais folga.")
            desenhar(estado["area"])
            return
        estado["area"] = area
        aviso.config(text="")
        salvar.config(state="normal")
        desenhar(area)

    def fim(area):
        estado["resultado"] = area
        raiz.destroy()

    tela.bind("<ButtonPress-1>", apertou)
    tela.bind("<B1-Motion>", arrastou)
    tela.bind("<ButtonRelease-1>", soltou)
    fonte = ("Segoe UI", 11)
    tk.Button(barra, text="Cancelar", font=fonte, command=raiz.destroy).pack(side="right", padx=4)
    tk.Button(barra, text=t["toda"], font=fonte, command=lambda: fim([])).pack(side="right", padx=4)
    salvar = tk.Button(barra, text="Salvar", font=fonte + ("bold",), command=lambda: fim(estado["area"]),
                       state="normal" if estado["area"] else "disabled")
    salvar.pack(side="right", padx=4)
    desenhar(estado["area"])
    raiz.mainloop()
    if estado["resultado"] is None:
        print("Cancelado: nada mudou.")
        return None
    cfg[chave] = estado["resultado"]
    salvar_config(cfg)
    previa = PASTA / ("area-marcada.jpg" if chave == "area" else "ponto-marcado.jpg")
    cv2.imwrite(str(previa), desenhar_area(img, cfg[chave]))
    if cfg[chave]:
        lw, lh = round((cfg[chave][2] - cfg[chave][0]) * w), round((cfg[chave][3] - cfg[chave][1]) * h)
        print(f"Salvo ({lw}x{lh} pixels). {t['salvo']}")
    else:
        print(t["tudo"])
    print(f"Foto com o lugar marcado: {previa}")
    return cfg[chave]


MODOS = {"mao": "Placa mostrada na mão", "carros": "Carros entrando e saindo pelo portão"}


def escolher_modo(cfg, modo=""):
    """O que o leitor conta: a placa mostrada na mão (padrão) ou os carros entrando e saindo."""
    if not modo:
        atual = cfg.get("contar", "mao")
        print("O que o leitor de placas conta?")
        print(f"  1) {MODOS['mao']}: mostrem a placa perto da câmera. Carro que não chegou fica recebido;")
        print("     carro que já está na oficina: o painel pergunta se ele está pronto.")
        print(f"  2) {MODOS['carros']}: para a câmera na pilastra, mirando o portão.")
        print(f"Agora: {MODOS.get(atual, atual)}.")
        r = input("Digite 1 ou 2 e aperte Enter (só Enter deixa como está): ").strip()
        modo = {"1": "mao", "2": "carros"}.get(r, atual)
    if modo not in MODOS:
        raise SystemExit("Modo inválido: use mao ou carros.")
    cfg["contar"] = modo
    salvar_config(cfg)
    print(f"Pronto: {MODOS[modo]}. O leitor ligado já passa a usar.")
    if modo == "mao" and not cfg.get("ponto"):
        print("Dica: abra o marcar-ponto.bat e marque o lugar onde vocês vão mostrar a placa.")
    return modo


# ---------- Verificar ----------

def verificar(cfg):
    ok = True
    falta = faltando(cfg)
    print("Configuração:", "OK" if not falta else "falta " + " e ".join(falta))
    ok &= not falta
    if cfg.get("camera"):
        img, erro = imagem_e_erro(cfg["camera"])
        de = " (janela do CMS)" if str(cfg["camera"]).startswith("tela:") else ""
        print("Câmera:", f"OK{de} ({img.shape[1]}x{img.shape[0]})" if img is not None else f"não abriu{de}. {erro}")
        ok &= img is not None
        if img is not None and not cfg.get("area"):
            print("Área dos carros: não marcada (o marcar-area.bat ajuda a ler placas pequenas)")
    else:
        print("Câmera: ainda não procurada (rode o procurar-camera.bat ou o usar-cms.bat)")
        ok = False
    if not falta:
        try:
            r = enviar(cfg, {"acao": "camera_ping", "camera": True, "erro": "", "modo": cfg.get("contar", "mao")})
            print("Planilha:", "OK" if r.get("ok") else "não aceitou a chave (baixe a configuração de novo no painel)")
            ok &= bool(r.get("ok"))
        except Exception as e:
            print("Planilha: sem conexão", e)
            ok = False
    return ok


def main(args=None):
    preparar_saida()
    p = argparse.ArgumentParser(description="Leitor de placas da Blin Car")
    p.add_argument("--procurar", nargs="?", const="", metavar="SENHA", help="acha a câmera na rede (opcional: a senha dela)")
    p.add_argument("--ip", default="", help="com --procurar: o IP da câmera, se já souber")
    p.add_argument("--testar", metavar="ARQUIVO", help="lê as placas de uma foto ou vídeo")
    p.add_argument("--preparar", action="store_true", help="baixa os arquivos da leitura de placas")
    p.add_argument("--verificar", action="store_true", help="confere configuração, câmera e planilha")
    p.add_argument("--tela", nargs="?", const=TITULO_CMS, metavar="TÍTULO",
                   help=f'lê as placas na janela do CMS (padrão: "{TITULO_CMS}")')
    p.add_argument("--area", action="store_true", help="marca com o mouse onde os carros passam")
    p.add_argument("--ponto", action="store_true", help="marca com o mouse onde vocês mostram a placa na mão")
    p.add_argument("--modo", nargs="?", const="", metavar="mao|carros", help="o que conta: placa na mão ou carros")
    p.add_argument("--escondido", action="store_true", help="sem janela (início com o Windows)")
    a = p.parse_args(args)
    cfg = ler_config()
    if a.preparar:
        print("Baixando os arquivos da leitura de placas (só na primeira vez)...")
        carregar_alpr(cfg)
        print("Pronto.")
        return 0
    if a.testar:
        testar(cfg, a.testar)
        return 0
    if a.procurar is not None:
        procurar(cfg, a.ip, a.procurar)
        return 0
    if a.verificar:
        return 0 if verificar(cfg) else 1
    if a.tela is not None:
        usar_tela(cfg, a.tela)
        return 0
    if a.area:
        marcar_area(cfg)
        return 0
    if a.ponto:
        marcar_area(cfg, "ponto")
        return 0
    if a.modo is not None:
        escolher_modo(cfg, a.modo)
        return 0
    configurar_log(console=not a.escondido)
    try:
        rodar(cfg)
    except SystemExit as e:
        # Sem janela (início com o Windows), o motivo fica no leitor.log.
        if e.code and not isinstance(e.code, int):
            log.info(str(e.code))
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
