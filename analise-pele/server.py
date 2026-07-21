# =========================
# Análise de Pele IA — Instituto Cardo
# Backend FastAPI + Claude API (visão computacional)
# =========================
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import io

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas as pdfcanvas

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "leads.db"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
WHATSAPP_NUMERO = os.environ.get("WHATSAPP_NUMERO", "")  # ex: 5511999999999
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_ANALISES_POR_IP_HORA = int(os.environ.get("MAX_ANALISES_POR_IP_HORA", "6"))

app = FastAPI(title="Análise de Pele IA — Instituto Cardo")

# Cliente criado sob demanda: se a ANTHROPIC_API_KEY faltar, o site continua
# no ar e o erro aparece apenas (e claramente) na hora da análise.
_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(503, "Serviço de análise não configurado (ANTHROPIC_API_KEY ausente).")
        _client = anthropic.Anthropic()
    return _client


# =========================
# Banco de leads (SQLite)
# =========================
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                criado_em TEXT NOT NULL,
                nome TEXT NOT NULL,
                whatsapp TEXT NOT NULL,
                email TEXT,
                consentimento INTEGER NOT NULL,
                score_geral INTEGER,
                resumo TEXT
            )
            """
        )


init_db()


def salvar_lead(nome: str, whatsapp: str, email: str, consentimento: bool) -> int:
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            "INSERT INTO leads (criado_em, nome, whatsapp, email, consentimento) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), nome, whatsapp, email, int(consentimento)),
        )
        return cur.lastrowid


def atualizar_lead_resultado(lead_id: int, score_geral: int, resumo: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE leads SET score_geral = ?, resumo = ? WHERE id = ?",
            (score_geral, resumo, lead_id),
        )


# =========================
# Rate limit simples por IP
# =========================
_janela_ip: dict[str, list[float]] = {}


def checar_rate_limit(ip: str) -> None:
    agora = time.time()
    historico = [t for t in _janela_ip.get(ip, []) if agora - t < 3600]
    if len(historico) >= MAX_ANALISES_POR_IP_HORA:
        raise HTTPException(429, "Limite de análises atingido. Tente novamente mais tarde.")
    historico.append(agora)
    _janela_ip[ip] = historico


# =========================
# Modelos de requisição
# =========================
class Foto(BaseModel):
    tipo: str = Field(description="frente | esquerda | direita")
    media_type: str
    imagem_base64: str


class Lead(BaseModel):
    nome: str
    whatsapp: str
    email: Optional[str] = ""
    consentimento: bool


class PedidoAnalise(BaseModel):
    lead: Lead
    fotos: List[Foto]


# =========================
# Esquema estruturado da análise
# =========================
DIMENSAO_SCHEMA = {
    "type": "object",
    "properties": {
        "nome": {"type": "string"},
        "score": {"type": "integer", "description": "0 a 100, sendo 100 excelente"},
        "nivel": {"type": "string", "enum": ["excelente", "bom", "atencao", "cuidado"]},
        "observacao": {"type": "string", "description": "1-2 frases, em português do Brasil, tom acolhedor"},
    },
    "required": ["nome", "score", "nivel", "observacao"],
    "additionalProperties": False,
}

ANALISE_SCHEMA = {
    "type": "object",
    "properties": {
        "qualidade_foto_ok": {
            "type": "boolean",
            "description": "false se as fotos não permitem análise (rosto não visível, muito escuras, desfocadas)",
        },
        "motivo_foto_ruim": {"type": "string"},
        "score_geral": {"type": "integer", "description": "0 a 100"},
        "tipo_pele_aparente": {
            "type": "string",
            "enum": ["seca", "normal", "mista", "oleosa", "indeterminado"],
        },
        "resumo": {"type": "string", "description": "2-3 frases resumindo o estado geral da pele, tom positivo e acolhedor"},
        "dimensoes": {"type": "array", "items": DIMENSAO_SCHEMA},
        "pontos_fortes": {"type": "array", "items": {"type": "string"}},
        "cuidados_recomendados": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Cuidados diários em casa (skincare), 3 a 5 itens",
        },
        "procedimentos_sugeridos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "para_que_serve": {"type": "string"},
                },
                "required": ["nome", "para_que_serve"],
                "additionalProperties": False,
            },
            "description": "2 a 4 procedimentos estéticos que uma clínica de estética avançada pode oferecer para os pontos de atenção identificados",
        },
    },
    "required": [
        "qualidade_foto_ok",
        "motivo_foto_ruim",
        "score_geral",
        "tipo_pele_aparente",
        "resumo",
        "dimensoes",
        "pontos_fortes",
        "cuidados_recomendados",
        "procedimentos_sugeridos",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Você é a assistente de análise de pele do Instituto Cardo, uma clínica de estética avançada brasileira.

Sua tarefa: analisar fotos do rosto enviadas pela pessoa e produzir uma avaliação ESTÉTICA (não médica) da pele, em português do Brasil, com tom acolhedor, positivo e profissional — como uma esteticista experiente conversando com uma cliente.

Analise estas dimensões (use exatamente estes nomes no campo "nome"):
1. "Linhas e rugas" — linhas finas, rugas de expressão, sulcos
2. "Manchas e uniformidade" — hiperpigmentação, melasma aparente, tom irregular
3. "Textura" — aspereza, cicatrizes de acne, irregularidades
4. "Poros" — dilatação aparente dos poros
5. "Oleosidade aparente" — brilho, aspecto oleoso ou ressecado
6. "Hidratação aparente" — viço, aspecto de pele hidratada ou desidratada
7. "Olheiras e área dos olhos" — escurecimento, bolsas aparentes
8. "Firmeza" — flacidez aparente, contorno facial

Regras importantes:
- Scores: 100 = excelente, 0 = precisa de muito cuidado. Seja honesta mas gentil — a pessoa deve se sentir acolhida, nunca julgada.
- Sempre encontre pontos fortes genuínos para elogiar.
- NUNCA diagnostique doenças (melanoma, câncer, rosácea como diagnóstico, etc.). Se notar algo que mereça avaliação profissional, diga apenas de forma leve que "vale uma avaliação presencial".
- Considere que iluminação e qualidade da foto afetam a aparência — seja moderada nas conclusões.
- Se as fotos não mostrarem um rosto humano claramente visível, marque qualidade_foto_ok = false e explique o motivo de forma simpática.
- Os procedimentos sugeridos devem ser de estética avançada oferecidos pela clínica (ex.: limpeza de pele profunda, Laser Lavieen para manchas e rejuvenescimento, microagulhamento, bioestimulador de colágeno, skinbooster, radiofrequência) — sem prometer resultados. Dê preferência ao Laser Lavieen quando houver manchas, textura irregular ou fotoenvelhecimento.
- Não mencione marcas de produtos de skincare (o Laser Lavieen, equipamento da clínica, pode e deve ser citado)."""


def analisar_com_claude(fotos: List[Foto]) -> dict:
    conteudo: list[dict] = []
    rotulos = {"frente": "Foto de frente", "esquerda": "Perfil esquerdo", "direita": "Perfil direito"}
    for foto in fotos:
        conteudo.append({"type": "text", "text": rotulos.get(foto.tipo, "Foto")})
        conteudo.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": foto.media_type,
                    "data": foto.imagem_base64,
                },
            }
        )
    conteudo.append(
        {
            "type": "text",
            "text": "Analise a pele do rosto nestas fotos e produza a avaliação estética completa no formato solicitado.",
        }
    )

    response = get_client().messages.create(
        model=MODEL,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": ANALISE_SCHEMA}},
        messages=[{"role": "user", "content": conteudo}],
    )

    if response.stop_reason == "refusal":
        raise HTTPException(422, "Não foi possível analisar estas fotos. Tente novamente com outra foto do rosto.")

    texto = next(b.text for b in response.content if b.type == "text")
    return json.loads(texto)


# =========================
# Rotas
# =========================
@app.post("/api/analisar")
def analisar(pedido: PedidoAnalise, request: Request):
    if not pedido.lead.consentimento:
        raise HTTPException(400, "É necessário aceitar os termos para continuar.")
    if not pedido.fotos:
        raise HTTPException(400, "Envie ao menos uma foto de frente.")
    if len(pedido.fotos) > 3:
        raise HTTPException(400, "Máximo de 3 fotos.")

    nome = pedido.lead.nome.strip()[:120]
    whatsapp = re.sub(r"\D", "", pedido.lead.whatsapp)[:15]
    email = (pedido.lead.email or "").strip()[:200]
    if not nome or len(whatsapp) < 10:
        raise HTTPException(400, "Preencha nome e WhatsApp válidos.")

    for foto in pedido.fotos:
        if foto.media_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(400, "Formato de imagem não suportado.")
        if len(foto.imagem_base64) > 7_000_000:  # ~5 MB decodificados
            raise HTTPException(400, "Imagem muito grande. Tente novamente.")
        try:
            base64.b64decode(foto.imagem_base64[:100])
        except Exception:
            raise HTTPException(400, "Imagem inválida.")

    ip = request.client.host if request.client else "?"
    checar_rate_limit(ip)

    # O lead é salvo ANTES da análise — mesmo que a IA falhe, o contato fica registrado
    lead_id = salvar_lead(nome, whatsapp, email, pedido.lead.consentimento)

    try:
        analise = analisar_com_claude(pedido.fotos)
    except HTTPException:
        raise
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Serviço de análise indisponível no momento ({e.status_code}). Tente em instantes.")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Serviço de análise indisponível no momento. Tente em instantes.")

    if analise.get("qualidade_foto_ok"):
        atualizar_lead_resultado(lead_id, int(analise.get("score_geral", 0)), analise.get("resumo", ""))

    analise["whatsapp_clinica"] = WHATSAPP_NUMERO
    return analise


# =========================
# Relatório em PDF
# =========================
VERDE = HexColor("#333134")
VERDE_ESC = HexColor("#232124")
DOURADO = HexColor("#8d6a97")
DOURADO_CL = HexColor("#c5aed0")
CREME = HexColor("#faf8f5")
TRILHA = HexColor("#e9e4ee")
TEXTO = HexColor("#39363b")
SUAVE = HexColor("#7a7580")
COR_NIVEL = {
    "excelente": HexColor("#71865a"),
    "bom": HexColor("#8d6a97"),
    "atencao": HexColor("#c98a3d"),
    "cuidado": HexColor("#b2472f"),
}
TXT_NIVEL = {"excelente": "Excelente", "bom": "Bom", "atencao": "Ponto de atenção", "cuidado": "Merece cuidado"}
TIPOS_PELE = {
    "seca": "Tendência a pele seca", "normal": "Pele de aspecto normal",
    "mista": "Tendência a pele mista", "oleosa": "Tendência a pele oleosa",
    "indeterminado": "Tipo de pele a confirmar",
}


def _whatsapp_bonito(numero: str) -> str:
    n = re.sub(r"\D", "", numero)
    if len(n) == 13 and n.startswith("55"):
        return f"+55 ({n[2:4]}) {n[4:9]}-{n[9:]}"
    return numero


def _texto_quebrado(c, texto, x, y, largura, fonte, tamanho, cor, entrelinha=None, max_linhas=None):
    """Desenha texto com quebra de linha e retorna o novo y."""
    entrelinha = entrelinha or tamanho + 3.5
    linhas = simpleSplit(texto, fonte, tamanho, largura)
    if max_linhas and len(linhas) > max_linhas:
        linhas = linhas[:max_linhas]
        linhas[-1] = linhas[-1].rstrip(".,;") + "…"
    c.setFont(fonte, tamanho)
    c.setFillColor(cor)
    for linha in linhas:
        c.drawString(x, y, linha)
        y -= entrelinha
    return y


def _titulo_secao(c, texto, y):
    c.setFont("Helvetica-Bold", 9.5)
    c.setFillColor(DOURADO)
    c.drawString(40, y, texto.upper())
    c.setStrokeColor(TRILHA)
    c.setLineWidth(0.7)
    c.line(40, y - 6, 555, y - 6)
    return y - 24


def _rodape(c, pagina):
    c.setFont("Helvetica", 7.5)
    c.setFillColor(SUAVE)
    c.drawCentredString(297.5, 26, "Instituto Cardo · Aesthetic — institutocardo.com.br")
    c.drawRightString(555, 26, f"{pagina}/2")


ROTULO_CURTO = {
    "Linhas e rugas": "Linhas", "Manchas e uniformidade": "Manchas", "Textura": "Textura",
    "Poros": "Poros", "Oleosidade aparente": "Oleosidade", "Hidratação aparente": "Hidratação",
    "Olheiras e área dos olhos": "Olhos", "Firmeza": "Firmeza",
}


def _radar(c, dimensoes, cx, cy, r):
    """Gráfico de radar octogonal com os scores das dimensões."""
    import math

    n = len(dimensoes)
    if n < 3:
        return

    def ponto(i, raio):
        # eixo Y do PDF cresce para cima — sinal invertido para a 1ª dimensão ficar no topo
        ang = -math.pi / 2 + (2 * math.pi * i) / n
        return cx + raio * math.cos(ang), cy - raio * math.sin(ang)

    def poligono(pontos, stroke, fill=0):
        p = c.beginPath()
        p.moveTo(*pontos[0])
        for pt in pontos[1:]:
            p.lineTo(*pt)
        p.close()
        c.drawPath(p, stroke=stroke, fill=fill)

    # anéis de referência e raios
    c.setLineWidth(0.7)
    c.setStrokeColor(HexColor("#ddd3e3"))
    for frac in (0.25, 0.5, 0.75, 1.0):
        poligono([ponto(i, r * frac) for i in range(n)], stroke=1)
    for i in range(n):
        x, y = ponto(i, r)
        c.line(cx, cy, x, y)

    # polígono dos dados (preenchimento translúcido)
    dados = [ponto(i, r * max(8, min(100, int(d.get("score", 0)))) / 100) for i, d in enumerate(dimensoes)]
    c.saveState()
    c.setFillColor(DOURADO)
    c.setFillAlpha(0.25)
    c.setStrokeColor(DOURADO)
    c.setLineWidth(1.8)
    c.setLineJoin(1)
    poligono(dados, stroke=1, fill=1)
    c.restoreState()
    c.setFillColor(DOURADO)
    for x, y in dados:
        c.circle(x, y, 2.6, stroke=0, fill=1)

    # rótulos
    c.setFont("Helvetica", 8.5)
    c.setFillColor(SUAVE)
    for i, d in enumerate(dimensoes):
        x, y = ponto(i, r + 16)
        rotulo = ROTULO_CURTO.get(d.get("nome", ""), str(d.get("nome", "")).split(" ")[0])
        if abs(x - cx) < 8:
            c.drawCentredString(x, y if y > cy else y - 8, rotulo)
        elif x > cx:
            c.drawString(x, y - 3, rotulo)
        else:
            c.drawRightString(x, y - 3, rotulo)


def gerar_pdf(nome: str, foto_bytes: Optional[bytes], analise: dict) -> bytes:
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    W, H = A4  # 595 x 842

    # ============ PÁGINA 1 ============
    c.setFillColor(CREME)
    c.rect(0, 0, W, H, stroke=0, fill=1)

    # Cabeçalho
    c.setFillColor(VERDE)
    c.rect(0, H - 104, W, 104, stroke=0, fill=1)
    flor = BASE_DIR / "static" / "flor-branca.png"
    if flor.exists():
        c.drawImage(str(flor), 500, H - 92, width=52, height=62, mask="auto", preserveAspectRatio=True)
    c.setFillColor(DOURADO_CL)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(40, H - 34, "I N S T I T U T O   C A R D O   ·   A E S T H E T I C")
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Times-Bold", 25)
    c.drawString(40, H - 64, "Relatório de Análise de Pele")
    c.setFillColor(DOURADO_CL)
    c.setFont("Helvetica", 10)
    data = datetime.now().strftime("%d/%m/%Y")
    c.drawString(40, H - 86, f"Preparado para {nome}  ·  {data}")

    # Foto (esquerda)
    fx, fy, fw, fh = 40, 488, 150, 205
    if foto_bytes:
        try:
            img = ImageReader(io.BytesIO(foto_bytes))
            iw, ih = img.getSize()
            escala = max(fw / iw, fh / ih)
            dw, dh = iw * escala, ih * escala
            c.saveState()
            p = c.beginPath()
            p.roundRect(fx, fy, fw, fh, 12)
            c.clipPath(p, stroke=0, fill=0)
            c.drawImage(img, fx - (dw - fw) / 2, fy - (dh - fh) / 2, dw, dh)
            c.restoreState()
            c.setStrokeColor(DOURADO)
            c.setLineWidth(1.2)
            c.roundRect(fx, fy, fw, fh, 12, stroke=1, fill=0)
            c.setFont("Helvetica", 7.5)
            c.setFillColor(SUAVE)
            c.drawCentredString(fx + fw / 2, fy - 12, "Foto analisada")
        except Exception:
            pass

    # Score (donut) + tipo de pele
    score = max(0, min(100, int(analise.get("score_geral", 0))))
    cx, cy, r = 280, 610, 48
    c.setLineCap(1)
    c.setLineWidth(9)
    c.setStrokeColor(TRILHA)
    c.circle(cx, cy, r, stroke=1, fill=0)
    if score > 0:
        p = c.beginPath()
        p.arc(cx - r, cy - r, cx + r, cy + r, 90, -(score / 100) * 360)
        c.setStrokeColor(DOURADO)
        c.drawPath(p, stroke=1, fill=0)
    c.setFillColor(VERDE)
    c.setFont("Times-Bold", 34)
    c.drawCentredString(cx, cy - 10, str(score))
    c.setFont("Helvetica", 6.8)
    c.setFillColor(SUAVE)
    c.drawCentredString(cx, cy - 26, "SCORE DA PELE")

    tipo = TIPOS_PELE.get(analise.get("tipo_pele_aparente", ""), "")
    if tipo:
        c.setFont("Helvetica-Bold", 9)
        lw = c.stringWidth(tipo, "Helvetica-Bold", 9) + 24
        px = 355 + (200 - lw) / 2 if lw < 200 else 355
        c.setFillColor(HexColor("#ece6f0"))
        c.roundRect(px, cy - 9, lw, 22, 11, stroke=0, fill=1)
        c.setFillColor(VERDE)
        c.drawCentredString(px + lw / 2, cy - 2, tipo)

    # Resumo
    resumo = analise.get("resumo", "")
    _texto_quebrado(c, f"“{resumo}”", 220, 528, 335, "Times-Italic", 11.5, TEXTO, 15, max_linhas=4)

    # Dimensões (barras)
    y = _titulo_secao(c, "Análise detalhada", 448)
    for d in (analise.get("dimensoes") or [])[:8]:
        nome_d = str(d.get("nome", ""))[:40]
        s = max(0, min(100, int(d.get("score", 0))))
        nivel = d.get("nivel", "bom")
        cor = COR_NIVEL.get(nivel, DOURADO)
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(TEXTO)
        c.drawString(40, y, nome_d)
        c.setFont("Helvetica", 8.5)
        c.setFillColor(SUAVE)
        c.drawRightString(555, y, f"{TXT_NIVEL.get(nivel, '')}  ·  {s}")
        c.setFillColor(TRILHA)
        c.roundRect(40, y - 13, 515, 6.5, 3.2, stroke=0, fill=1)
        c.setFillColor(cor)
        c.roundRect(40, y - 13, max(10, 515 * s / 100), 6.5, 3.2, stroke=0, fill=1)
        obs = str(d.get("observacao", ""))
        linhas = simpleSplit(obs, "Helvetica", 8, 515)
        if linhas:
            c.setFont("Helvetica", 8)
            c.setFillColor(SUAVE)
            c.drawString(40, y - 26, linhas[0] + ("…" if len(linhas) > 1 else ""))
        y -= 46
    _rodape(c, 1)
    c.showPage()

    # ============ PÁGINA 2 ============
    c.setFillColor(CREME)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(VERDE)
    c.rect(0, H - 56, W, 56, stroke=0, fill=1)
    if flor.exists():
        c.drawImage(str(flor), 297.5 - 14, H - 48, width=28, height=33, mask="auto", preserveAspectRatio=True)
    c.setFillColor(DOURADO_CL)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(40, H - 34, "I N S T I T U T O   C A R D O")
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Times-Italic", 11)
    c.drawRightString(555, H - 35, "Relatório de Análise de Pele")

    y = _titulo_secao(c, "Mapa da sua pele", H - 92)
    _radar(c, (analise.get("dimensoes") or [])[:8], 297.5, y - 90, 72)
    y -= 200

    y = _titulo_secao(c, "Seus pontos fortes", y)
    for p_forte in (analise.get("pontos_fortes") or [])[:3]:
        c.setFillColor(DOURADO)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(44, y, "•")
        y = _texto_quebrado(c, str(p_forte), 58, y, 495, "Helvetica", 9.5, TEXTO, 13, max_linhas=1) - 5

    y = _titulo_secao(c, "Cuidados recomendados em casa", y - 10)
    for i, cuidado in enumerate((analise.get("cuidados_recomendados") or [])[:4], 1):
        c.setFillColor(DOURADO)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(44, y, f"{i}.")
        y = _texto_quebrado(c, str(cuidado), 58, y, 495, "Helvetica", 9.5, TEXTO, 13, max_linhas=1) - 5

    y = _titulo_secao(c, "O que pode potencializar seus resultados", y - 10)
    for proc in (analise.get("procedimentos_sugeridos") or [])[:3]:
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(VERDE)
        c.drawString(44, y, str(proc.get("nome", ""))[:60])
        y -= 13
        y = _texto_quebrado(c, str(proc.get("para_que_serve", "")), 44, y, 510, "Helvetica", 9, SUAVE, 12.5, max_linhas=1) - 7

    # Caixa de agendamento
    box_h = 92
    by = max(96, y - box_h - 6)
    c.setFillColor(VERDE)
    c.roundRect(40, by, 515, box_h, 14, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Times-Bold", 15)
    c.drawCentredString(297.5, by + box_h - 30, "Pronta para transformar essa análise em um plano real?")
    c.setFont("Helvetica", 9.5)
    c.setFillColor(DOURADO_CL)
    c.drawCentredString(297.5, by + box_h - 48, "Agende uma avaliação presencial gratuita com nossas especialistas.")
    if WHATSAPP_NUMERO:
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(DOURADO_CL)
        c.drawCentredString(297.5, by + 18, f"WhatsApp: {_whatsapp_bonito(WHATSAPP_NUMERO)}")

    _texto_quebrado(
        c,
        "Esta análise é gerada por inteligência artificial com base nas fotos enviadas e tem caráter "
        "informativo e estético. Ela não substitui avaliação presencial nem constitui diagnóstico médico ou dermatológico.",
        40, 62, 515, "Helvetica", 7.2, SUAVE, 9.5,
    )
    _rodape(c, 2)
    c.showPage()
    c.save()
    return buf.getvalue()


class PedidoRelatorio(BaseModel):
    nome: str
    foto_base64: Optional[str] = None
    analise: dict


@app.post("/api/relatorio")
def relatorio(pedido: PedidoRelatorio):
    nome = pedido.nome.strip()[:120] or "Você"
    if not isinstance(pedido.analise, dict) or "score_geral" not in pedido.analise:
        raise HTTPException(400, "Análise inválida.")
    foto_bytes = None
    if pedido.foto_base64:
        if len(pedido.foto_base64) > 7_000_000:
            raise HTTPException(400, "Imagem muito grande.")
        try:
            foto_bytes = base64.b64decode(pedido.foto_base64)
        except Exception:
            foto_bytes = None
    pdf = gerar_pdf(nome, foto_bytes, pedido.analise)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="analise-de-pele-instituto-cardo.pdf"'},
    )


@app.get("/api/leads.csv", response_class=PlainTextResponse)
def exportar_leads(token: str = ""):
    """Exportação simples dos leads capturados (protegida por ADMIN_TOKEN)."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Acesso negado.")
    with sqlite3.connect(DB_PATH) as db:
        linhas = db.execute(
            "SELECT id, criado_em, nome, whatsapp, email, score_geral, resumo FROM leads ORDER BY id DESC"
        ).fetchall()
    saida = ["id;criado_em;nome;whatsapp;email;score_geral;resumo"]
    for linha in linhas:
        saida.append(";".join("" if c is None else str(c).replace(";", ",").replace("\n", " ") for c in linha))
    return "\n".join(saida)


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
