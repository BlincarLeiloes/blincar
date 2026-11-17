"""Placas brasileiras: limpar a leitura, juntar as várias leituras de um carro passando e saber se ele se aproxima ou se afasta.

Placa antiga: ABC1234. Mercosul: ABC1D23. As 3 primeiras são letras, a 4ª, a 6ª e a 7ª são números e a 5ª pode ser
letra (Mercosul) ou número (antiga).
"""
import re
from collections import Counter, defaultdict

FORMATO = re.compile(r"^[A-Z]{3}\d[A-Z0-9]\d{2}$")
# A leitura às vezes troca letra e número parecidos: a posição na placa diz qual dos dois é.
LETRA_PARA_NUMERO = {"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "J": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "T": "7", "A": "4"}
NUMERO_PARA_LETRA = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B"}


def normalizar(texto):
    """'abc-1d23 ' vira 'ABC1D23'; 'A8C1D2S' vira 'ABC1D25'. Devolve '' se não der uma placa."""
    s = re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())
    if len(s) != 7:
        return ""
    saida = []
    for i, c in enumerate(s):
        if i < 3:
            c = NUMERO_PARA_LETRA.get(c, c)
        elif i in (3, 5, 6):
            c = LETRA_PARA_NUMERO.get(c, c)
        saida.append(c)
    s = "".join(saida)
    return s if FORMATO.match(s) else ""


def mercosul(placa):
    """A antiga ABC1234 vira ABC1C34 (o 5º número vira letra, como no Detran). Serve para comparar."""
    p = normalizar(placa)
    if p and p[4].isdigit():
        p = p[:4] + "ABCDEFGHIJ"[int(p[4])] + p[5:]
    return p


def diferenca(a, b):
    """Quantas posições diferentes entre duas placas (comparando no formato Mercosul)."""
    a, b = mercosul(a), mercosul(b)
    if not a or not b:
        return 7
    return sum(1 for x, y in zip(a, b) if x != y)


def votar(leituras):
    """Junta as leituras de um carro: em cada posição ganha a letra com mais confiança somada.

    leituras: lista de (placa, confianca). Devolve (placa, confianca_media)."""
    if not leituras:
        return "", 0.0
    votos = [defaultdict(float) for _ in range(7)]
    for placa, conf in leituras:
        for i, c in enumerate(placa):
            votos[i][c] += max(float(conf), 0.01)
    placa = "".join(max(v.items(), key=lambda kv: kv[1])[0] for v in votos)
    placa = normalizar(placa) or Counter(p for p, _ in leituras).most_common(1)[0][0]
    confs = [float(c) for p, c in leituras if p == placa] or [float(c) for _, c in leituras]
    return placa, sum(confs) / len(confs)


def direcao(larguras):
    """Pela largura da placa na imagem ao longo do tempo: 'aproximando' (cresce), 'afastando' (diminui) ou ''."""
    if len(larguras) < 3:
        return ""
    n = len(larguras)
    terco = max(1, n // 3)
    inicio = sum(larguras[:terco]) / terco
    fim = sum(larguras[-terco:]) / terco
    if inicio <= 0:
        return ""
    razao = fim / inicio
    if razao >= 1.15:
        return "aproximando"
    if razao <= 0.87:
        return "afastando"
    return ""


class Passagens:
    """Agrupa as leituras de cada carro que passa. Um carro "termina de passar" quando fica `pausa` segundos sem ser lido.

    adicionar(placa, conf, largura, agora, extra) e terminadas(agora) -> lista de dicts prontos para mandar."""

    def __init__(self, pausa=4.0, min_leituras=2, conf_sozinha=0.9):
        self.pausa = pausa
        self.min_leituras = min_leituras
        self.conf_sozinha = conf_sozinha
        self.ativas = []

    def adicionar(self, placa, conf, largura, agora, extra=None):
        placa = normalizar(placa)
        if not placa:
            return None
        alvo = None
        for p in self.ativas:
            if any(diferenca(placa, x) <= 1 for x, _ in p["leituras"][-6:]):
                alvo = p
                break
        if alvo is None:
            alvo = {"inicio": agora, "fim": agora, "leituras": [], "larguras": [], "melhor": None, "melhor_conf": -1}
            self.ativas.append(alvo)
        alvo["leituras"].append((placa, float(conf)))
        alvo["larguras"].append(float(largura))
        alvo["fim"] = agora
        if float(conf) > alvo["melhor_conf"]:
            alvo["melhor_conf"] = float(conf)
            alvo["melhor"] = extra
        return alvo

    def terminadas(self, agora, forcar=False):
        prontas, ficam = [], []
        for p in self.ativas:
            (prontas if forcar or agora - p["fim"] >= self.pausa else ficam).append(p)
        self.ativas = ficam
        saida = []
        for p in prontas:
            placa, conf = votar(p["leituras"])
            n = len(p["leituras"])
            if not placa or (n < self.min_leituras and conf < self.conf_sozinha):
                continue
            saida.append({"placa": placa, "confianca": round(conf, 3), "leituras": n, "direcao": direcao(p["larguras"]),
                          "inicio": p["inicio"], "fim": p["fim"], "melhor": p["melhor"]})
        return saida
