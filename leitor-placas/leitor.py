"""Leitor de placas da Blin Car.

Olha a câmera do portão, lê as placas dos carros que passam e manda para a planilha (que marca a chegada e a saída
na agenda). Fica rodando no computador da oficina.

Uso (os arquivos .bat da pasta fazem isso):
  python leitor.py               liga o leitor (fica rodando)
  python leitor.py --procurar    acha a câmera na rede e guarda o endereço dela
  python leitor.py --testar X    lê as placas de uma foto ou vídeo (não manda nada)
  python leitor.py --preparar    baixa os arquivos da leitura de placas (uma vez, na instalação)
  python leitor.py --verificar   confere a configuração, a câmera e a planilha
"""
import argparse
import concurrent.futures
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import placas

VERSAO = "1.0"
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
            raise SystemExit(f"O arquivo {ARQ_CONFIG.name} está com defeito ({e}). Apague ele e baixe de novo no painel: Recebidos > Câmera.")
    baixada = config_baixada()
    if baixada and (baixada["chave"] != cfg.get("chave") or baixada["planilha"] != cfg.get("planilha")):
        cfg.update(planilha=baixada["planilha"], chave=baixada["chave"])
        salvar_config(cfg)
        print("Peguei a configuração que o painel baixou (pasta Downloads).")
    return cfg


def salvar_config(cfg):
    guardar = {k: v for k, v in cfg.items() if k in ("planilha", "chave", "camera", "area") or v != PADRAO.get(k)}
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
                log.info("A planilha não aceitou a chave. No painel, Recebidos > Câmera > \"Baixar a configuração de novo\" e troque o config-blincar.json.")
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
                    r = enviar(self.cfg, {"acao": "camera_ping", "camera": self.estado["camera_ok"], "erro": self.estado["camera_erro"]})
                    if r.get("ok") is False and r.get("erro") == "chave":
                        log.info("A planilha não aceitou a chave. Baixe a configuração de novo no painel (Recebidos > Câmera).")
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
    import cv2
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, tempo_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, tempo_ms])
    except TypeError:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    return cap


class Camera(threading.Thread):
    """Lê a câmera sem parar e guarda só a imagem mais nova (assim a leitura nunca fica atrasada)."""

    def __init__(self, url, estado):
        super().__init__(daemon=True)
        self.url, self.estado = url, estado
        self.trava = threading.Lock()
        self.quadro = None
        self.quando = 0.0
        self.parar = threading.Event()

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
                self._erro("Não consegui abrir a câmera. Confira se ela está ligada e no cabo do roteador.")
                time.sleep(espera)
                espera = min(espera * 2, 60)
                continue
            log.info("Câmera conectada.")
            espera = 2
            falhas = 0
            while not self.parar.is_set():
                ok, quadro = cap.read()
                if not ok or quadro is None:
                    falhas += 1
                    if falhas >= 25:
                        self._erro("A câmera parou de mandar imagem.")
                        break
                    time.sleep(0.2)
                    continue
                falhas = 0
                if ritmo:
                    time.sleep(ritmo)
                with self.trava:
                    self.quadro, self.quando = quadro, time.time()
                if not self.estado["camera_ok"]:
                    self.estado.update(camera_ok=True, camera_erro="")
            cap.release()
            time.sleep(2)

    def _erro(self, msg):
        if self.estado["camera_erro"] != msg:
            log.info(msg)
        self.estado.update(camera_ok=False, camera_erro=msg)


# ---------- Leitura das placas ----------

def carregar_alpr(cfg):
    from fast_alpr import ALPR
    return ALPR(detector_model=cfg["detector"], ocr_model=cfg["ocr"])


def recortar(quadro, area):
    """Só a parte da imagem onde o carro passa (area em frações). Devolve (recorte, x0, y0)."""
    if not area or len(area) != 4:
        return quadro, 0, 0
    h, w = quadro.shape[:2]
    x1, y1, x2, y2 = (int(round(v * t)) for v, t in zip(area, (w, h, w, h)))
    x1, y1 = max(0, min(x1, w - 1)), max(0, min(y1, h - 1))
    x2, y2 = max(x1 + 1, min(x2, w)), max(y1 + 1, min(y2, h))
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


def rodar(cfg):
    falta = faltando(cfg)
    if falta:
        raise SystemExit("Falta " + " e ".join(falta) + ". No painel: Recebidos > Câmera > \"Ligar a câmera\" baixa o config-blincar.json. "
                         "Coloque ele nesta pasta (ou deixe na pasta Downloads).")
    if not cfg.get("camera"):
        raise SystemExit("Ainda não sei o endereço da câmera. Rode o procurar-camera.bat primeiro.")
    try:
        trava = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        trava.bind(("127.0.0.1", PORTA_TRAVA))
    except OSError:
        raise SystemExit("O leitor já está ligado neste computador.")

    log.info(f"Leitor de placas Blin Car {VERSAO} ligando...")
    alpr = carregar_alpr(cfg)
    estado = {"camera_ok": False, "camera_erro": "Conectando na câmera..."}
    envio = Envio(cfg, estado)
    envio.start()
    cam = Camera(cfg["camera"], estado)
    cam.start()
    mov = Movimento()
    passagens = placas.Passagens(pausa=cfg["segundos_fim_passagem"], min_leituras=cfg["min_leituras"])
    ja_mandadas = {}
    intervalo = 1.0 / max(0.5, float(cfg["analises_por_segundo"]))
    ultimo_mov = 0.0
    ultima_limpeza = 0.0
    ultimo_quadro = 0.0
    log.info("Pronto. Olhando o portão (feche esta janela para desligar).")
    try:
        while True:
            inicio = time.time()
            if inicio - ultima_limpeza > 3600:
                ultima_limpeza = inicio
                apagar_fotos_antigas(cfg["dias_guardar_fotos"])
            quadro, quando = cam.ultimo()
            if quadro is not None and quando != ultimo_quadro:
                ultimo_quadro = quando
                parte, x0, y0 = recortar(quadro, cfg["area"])
                if mov.mexeu(parte):
                    ultimo_mov = inicio
                # Lê enquanto algo se mexe e mais 3 segundos depois.
                if inicio - ultimo_mov <= 3:
                    for placa, conf, (x1, y1, x2, y2) in ler_placas(alpr, parte, cfg["confianca_minima"]):
                        caixa = (x1 + x0, y1 + y0, x2 + x0, y2 + y0)
                        passagens.adicionar(placa, conf, x2 - x1, inicio, (quadro, caixa, datetime.now()))
            for p in passagens.terminadas(time.time()):
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
                log.info(f"Placa {p['placa']}  ({p['leituras']} leituras, {round(p['confianca'] * 100)}%, {p['direcao'] or 'sem direção'})  foto: {foto}")
                envio.mandar({"acao": "camera_evento", "placa": p["placa"], "confianca": p["confianca"], "leituras": p["leituras"],
                              "direcao": p["direcao"], "quando": quando_txt})
            time.sleep(max(0.0, intervalo - (time.time() - inicio)))
    except KeyboardInterrupt:
        log.info("Leitor desligado.")
    finally:
        cam.parar.set()
        envio.parar.set()


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
    passagens = placas.Passagens(pausa=cfg["segundos_fim_passagem"], min_leituras=cfg["min_leituras"])
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
            passagens.adicionar(placa, conf, x2 - x1, t, None)
            if melhor is None or conf > melhor[1]:
                melhor = (quadro.copy(), conf, placa, (x1 + x0, y1 + y0, x2 + x0, y2 + y0))
        resultado += passagens.terminadas(t)
    resultado += passagens.terminadas(n / fps + 999, forcar=True)
    for p in resultado:
        print(f"  {p['placa']}  ({p['leituras']} leituras, {round(p['confianca'] * 100)}%, {p['direcao'] or 'sem direção'}) aos {int(p['inicio'])}s")
    if not resultado:
        print("  Nenhuma placa lida nesse vídeo.")
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
PORTAS_OUTRAS = [80, 81, 443, 8000, 8080, 8800, 8899, 34567, 5000, 9527, 23]


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


def pega_imagem(url):
    cap = abrir_video(url, 5000)
    try:
        if not cap.isOpened():
            return None
        for _ in range(10):
            ok, q = cap.read()
            if ok and q is not None:
                return q
        return None
    finally:
        cap.release()


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


def portas_de_video(ip):
    """Portas desse aparelho que respondem como vídeo de câmera (RTSP)."""
    portas = [p for p in sorted(set(PORTAS_RTSP + PORTAS_OUTRAS)) if porta_aberta(ip, p, 1.5)]
    return [p for p in portas if any(resposta_rtsp(ip, c, p) > 0 for c in CAMINHOS_TESTE)], portas


def tentar_camera(cfg, ip, porta, usuarios):
    """Tenta abrir o vídeo nessa porta. Devolve (url, pediu_senha)."""
    import cv2
    codigos = {c: resposta_rtsp(ip, c, porta) for c in CAMINHOS}
    abertos = [c for c in CAMINHOS if codigos[c] == 200]
    pede = [c for c in CAMINHOS if codigos[c] == 401]
    tentativas = [(c, "", "") for c in abertos] + [(c, u, s) for u, s in usuarios if u for c in pede]
    for caminho, usuario, senha in tentativas:
        url = com_senha(ip, caminho, usuario, senha, porta)
        img = pega_imagem(url)
        if img is None:
            continue
        cfg["camera"] = url
        salvar_config(cfg)
        foto = PASTA / "foto-camera.jpg"
        cv2.imwrite(str(foto), img)
        print(f"\nACHEI A CÂMERA: {ip} (imagem de {img.shape[1]}x{img.shape[0]}).")
        print(f"Guardei o endereço no {ARQ_CONFIG.name} e tirei uma foto: {foto}")
        return url, False
    return None, bool(pede)


def anotar(texto):
    print(texto)
    with open(PASTA / "procurar.log", "a", encoding="utf-8") as f:
        f.write(texto + "\n")


def descobrir_camera(base, meu):
    """A câmera não respondeu como câmera: acha qual aparelho é ela, tirando da tomada e vendo quem some."""
    anotar(f"\n--- {datetime.now():%d/%m %H:%M} Descobrindo qual aparelho é a câmera ---")
    antes = aparelhos(base, meu)
    anotar(f"Aparelhos na rede agora ({len(antes)}):")
    for ip in sorted(antes, key=lambda x: int(x.split(".")[-1])):
        anotar(f"  {ip}   portas: {', '.join(map(str, sorted(antes[ip]))) or 'nenhuma das conhecidas'}")
    input("\nAgora TIRE A CÂMERA DA TOMADA, espere uns 20 segundos e aperte Enter...")
    depois = aparelhos(base, meu)
    sumiram = sorted(set(antes) - set(depois), key=lambda x: int(x.split(".")[-1]))
    input("Pode LIGAR A CÂMERA de novo. Espere 1 minuto (ela demora para ligar) e aperte Enter...")
    if not sumiram:
        anotar("Nenhum aparelho sumiu quando a câmera foi desligada: ela não está na mesma rede deste computador.\n"
               "  - Confira se o computador está na internet do MESMO roteador em que a câmera está no cabo\n"
               "    (se tiver repetidor ou duas redes, ligue o computador direto na rede do roteador);\n"
               "  - a câmera pode ter guardado o endereço da oficina antiga: segure o botão de reset dela por 10 segundos,\n"
               "    espere 1 minuto e rode o procurar-camera.bat de novo.")
        return None
    if len(sumiram) > 1:
        anotar(f"Sumiram {len(sumiram)} aparelhos: {', '.join(sumiram)}. Rode de novo (pode ter sido outro aparelho saindo da rede).")
        return None
    ip = sumiram[0]
    anotar(f"A câmera é o aparelho {ip}. Portas abertas: {', '.join(map(str, sorted(antes[ip]))) or 'nenhuma das conhecidas'}.")
    return ip


def procurar(cfg, ip_camera="", senha_extra=""):
    print("Procurando a câmera na rede (pode levar 1 ou 2 minutos)...")
    meu = meu_ip()
    if not meu and not ip_camera:
        raise SystemExit("Este computador não está em nenhuma rede. Conecte no Wi-Fi ou no cabo do roteador da oficina.")
    base = ".".join(meu.split(".")[:3])
    usuarios = list(USUARIOS)
    if senha_extra:
        usuarios = [("admin", senha_extra)] + usuarios
    if ip_camera:
        candidatos = {ip_camera: portas_de_video(ip_camera)[0]}
    else:
        print(f"Este computador: {meu}. Olhando os aparelhos de {base}.1 a {base}.254...")
        candidatos = varrer(base, meu, PORTAS_RTSP)
        # A câmera que já foi descoberta antes (tirando da tomada) entra com todas as portas de vídeo dela.
        conhecida = str(cfg.get("camera_ip") or "")
        if conhecida.startswith(base + ".") and conhecida not in candidatos:
            portas = portas_de_video(conhecida)[0]
            if portas:
                candidatos[conhecida] = portas
    pediu_senha = []
    for ip, portas in candidatos.items():
        for porta in portas:
            print(f"Aparelho com vídeo em {ip} (porta {porta}). Testando...")
            url, pede = tentar_camera(cfg, ip, porta, usuarios)
            if url:
                return url
            if pede:
                pediu_senha.append(ip)
    if pediu_senha:
        # Código 3: o procurar-camera.bat pergunta a senha e tenta de novo.
        print(f"A câmera em {', '.join(sorted(set(pediu_senha)))} pede senha." + (" Essa senha não abriu." if senha_extra else "") +
              " É a senha que vocês criaram no aplicativo da câmera.")
        raise SystemExit(3)
    if candidatos and not ip_camera:
        print("Achei aparelho com vídeo, mas não consegui abrir a imagem.")
    else:
        print("Não achei a câmera do jeito comum (porta de vídeo 554).")
    if ip_camera or not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("Confira se a câmera está ligada (fonte 12V) e com o cabo no roteador, e se este computador está na "
                         "internet do mesmo roteador. Depois rode o procurar-camera.bat de novo.")
    ip = descobrir_camera(base, meu)
    if not ip:
        raise SystemExit(1)
    cfg["camera_ip"] = ip
    salvar_config(cfg)
    # Achou o aparelho: vê se alguma porta dele responde como vídeo de câmera.
    portas_video, portas = portas_de_video(ip)
    for porta in portas_video:
        print(f"A porta {porta} de {ip} responde como vídeo. Testando...")
        url, pede = tentar_camera(cfg, ip, porta, usuarios)
        if url:
            return url
        if pede:
            print(f"A câmera em {ip} pede senha. É a senha que vocês criaram no aplicativo da câmera.")
            raise SystemExit(3)
    anotar(f"A câmera ({ip}) está na rede, mas não manda o vídeo para o computador: só para o aplicativo dela.\n"
           f"Portas abertas: {', '.join(map(str, portas)) or 'nenhuma'}. Tire um print desta janela e mande para o Arthur.")
    raise SystemExit(1)


# ---------- Verificar ----------

def verificar(cfg):
    ok = True
    falta = faltando(cfg)
    print("Configuração:", "OK" if not falta else "falta " + " e ".join(falta))
    ok &= not falta
    if cfg.get("camera"):
        img = pega_imagem(cfg["camera"])
        print("Câmera:", f"OK ({img.shape[1]}x{img.shape[0]})" if img is not None else "não abriu")
        ok &= img is not None
    else:
        print("Câmera: ainda não procurada (rode o procurar-camera.bat)")
        ok = False
    if not falta:
        try:
            r = enviar(cfg, {"acao": "camera_ping", "camera": True, "erro": ""})
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
