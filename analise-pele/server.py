# =========================
# Análise de Pele IA — Instituto Card
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

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "leads.db"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
WHATSAPP_NUMERO = os.environ.get("WHATSAPP_NUMERO", "")  # ex: 5511999999999
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
MAX_ANALISES_POR_IP_HORA = int(os.environ.get("MAX_ANALISES_POR_IP_HORA", "6"))

app = FastAPI(title="Análise de Pele IA — Instituto Card")

client = anthropic.Anthropic()


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

SYSTEM_PROMPT = """Você é a assistente de análise de pele do Instituto Card, uma clínica de estética avançada brasileira.

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
- Os procedimentos sugeridos devem ser genéricos de estética avançada (ex.: limpeza de pele profunda, peeling químico, microagulhamento, laser, bioestimulador de colágeno, skinbooster, radiofrequência) — sem prometer resultados.
- Não mencione marcas de produtos."""


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

    response = client.messages.create(
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
