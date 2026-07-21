# ✨ Análise de Pele com IA — Instituto Card

Ferramenta de **isca digital** para o site do Instituto Card (institutocard.com.br): a pessoa tira fotos do rosto, informa nome e WhatsApp, e recebe na hora uma análise estética da pele feita por inteligência artificial (Claude, da Anthropic) — score geral, 8 dimensões analisadas (rugas, manchas, textura, poros, oleosidade, hidratação, olheiras, firmeza), pontos fortes, cuidados recomendados e procedimentos sugeridos.

**Cada análise gera um lead** (nome + WhatsApp + e-mail + score) salvo no banco, pronto para o time comercial fazer o acompanhamento.

## Como funciona

1. **Hero / convite** — apresenta a análise gratuita
2. **Captura do lead** — nome, WhatsApp, e-mail (opcional) + consentimento LGPD, **antes** de entregar o valor
3. **Fotos guiadas** — frente (obrigatória) + perfis (opcionais), pela câmera ou galeria, com dicas de iluminação; as imagens são redimensionadas no navegador (nada pesado sobe pro servidor)
4. **Análise com IA** — as fotos vão para a API da Claude com visão computacional, que devolve a avaliação em formato estruturado
5. **Resultado** — score animado, barras por dimensão, recomendações e **CTA de agendamento pelo WhatsApp** já com mensagem pronta
6. As fotos **não são armazenadas** — só o lead e o score

## Rodando localmente

```bash
cd analise-pele
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...    # sua chave da https://platform.claude.com
export WHATSAPP_NUMERO=5511999999999   # WhatsApp da clínica
export ADMIN_TOKEN=um-token-secreto
uvicorn server:app --reload --port 8000
```

Abra http://localhost:8000

## Publicando (institutocard.com.br)

A forma mais simples é publicar em um **subdomínio** e linkar no site principal:

1. **Hospede o app** em um serviço com Python (Railway, Render, Fly.io ou uma VPS):
   - Comando de start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - Configure as variáveis de ambiente do `.env.example`
   - Se a hospedagem tiver disco persistente, aponte `DB_PATH` para ele (ex.: `/data/leads.db`) para não perder leads em re-deploys
2. **Crie o subdomínio** no DNS do institutocard.com.br:
   - `analise.institutocard.com.br` → CNAME apontando para a URL da hospedagem
3. **No site principal**, adicione um botão/banner chamativo:
   - "✨ Faça sua análise de pele gratuita com IA" → link para `https://analise.institutocard.com.br`
   - Também funciona muito bem como link na bio do Instagram e em anúncios

## Vendo os leads capturados

```
https://analise.institutocard.com.br/api/leads.csv?token=SEU_ADMIN_TOKEN
```

Retorna CSV com: data, nome, WhatsApp, e-mail, score e resumo da análise — dá para abrir direto no Excel/Google Sheets.

## Custos

Cada análise consome a API da Anthropic (fotos + resposta ≈ alguns centavos de dólar por análise, dependendo do modelo). Há um limite por IP (`MAX_ANALISES_POR_IP_HORA`, padrão 6) para evitar abuso.

## Avisos importantes

- A ferramenta entrega uma avaliação **estética e informativa** — o texto exibido ao usuário já inclui o aviso de que não é diagnóstico médico/dermatológico.
- O formulário inclui consentimento explícito (LGPD) para contato posterior.
