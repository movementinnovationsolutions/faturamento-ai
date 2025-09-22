# =========================
# insightcare_app.py — Movement Innovation Solutions
# =========================
from __future__ import annotations

import io, os, re, json, csv
from datetime import datetime
from typing import List, Tuple, Optional

import pandas as pd
import streamlit as st


# =========================
# Config da página
# =========================
st.set_page_config(page_title="InsightCare — Auditoria & Painéis (SUS + Privado)", layout="wide")
st.title("🧠 InsightCare — Auditoria & Painéis (SUS AIH/BPA/APAC + Privado TISS/TUSS)")
st.caption("Movement Innovation Solutions")


# =========================
# Helpers gerais
# =========================
PROC_10D = re.compile(r"(?<!\d)(\d{10})(?!\d)")
DATE_8D = re.compile(r"\b(\d{8})\b")

def try_read_text(file) -> str:
    """
    Lê binário e decodifica como texto.
    Tenta latin-1; se falhar, volta o ponteiro e tenta utf-8.
    """
    try:
        return file.getvalue().decode("latin-1", errors="ignore")
    except Exception:
        file.seek(0)
        return file.getvalue().decode("utf-8", errors="ignore")

def detect_jul_2025(text: str) -> bool:
    for token in DATE_8D.findall(text):
        for fmt in ("%d%m%Y", "%Y%m%d"):
            try:
                d = datetime.strptime(token, fmt)
                if d.year == 2025 and d.month == 7:
                    return True
            except Exception:
                continue
    return False

def extract_codes(text: str) -> List[str]:
    return PROC_10D.findall(text)

def round2(x) -> Optional[float]:
    try:
        return round(float(x), 2)
    except Exception:
        return None


# =========================
# Leitura robusta de CSV/Excel
# =========================
def read_csv_smart(file) -> pd.DataFrame:
    """
    Estratégia:
      1) tenta CSV com auto-separador (csv.Sniffer) em utf-8, depois latin-1
      2) tenta pandas.read_csv(sep=[',',';']) com encodings diferentes
      3) cai para Excel (read_excel)
    Retorna DataFrame (ou levanta exceção se impossível).
    """
    # snapshot bytes
    file.seek(0)
    raw = file.read()
    # para pandas novamente
    bio = io.BytesIO(raw)

    # 1) Sniffer
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc, errors="ignore")
            sample = text[:20000]
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
            sep = dialect.delimiter
            df = pd.read_csv(io.StringIO(text), sep=sep)
            return df
        except Exception:
            pass

    # 2) Tentativas diretas
    for enc in ("utf-8", "latin-1"):
        for sep in (",", ";", "\t", "|"):
            try:
                file_obj = io.BytesIO(raw)
                return pd.read_csv(file_obj, sep=sep, encoding=enc)
            except Exception:
                continue

    # 3) Excel
    try:
        return pd.read_excel(io.BytesIO(raw))
    except Exception as e:
        raise ValueError(f"Não foi possível ler o arquivo (CSV/Excel). Erro: {e}")


# =========================
# IA – priorização e plano de ação (Auditoria)
# =========================
def ia_priorizar_e_sugerir(findings_df_list: List[Tuple[str, pd.DataFrame]], meta: dict):
    """
    findings_df_list: lista [(fonte, df_findings)]
    meta: dict {"competencia": "..."}
    Tenta usar OpenAI se OPENAI_API_KEY existir; senão, resumo determinístico.
    """
    rows = []
    for fonte, df in findings_df_list:
        if df is None or df.empty:
            continue
        tmp = df.copy()
        tmp["fonte"] = fonte
        rows.append(tmp)

    if not rows:
        return {
            "resumo_md": "### Resumo Executivo (IA)\n\nNenhum achado relevante encontrado.",
            "acoes": pd.DataFrame(columns=[
                "prioridade","regra_id","gravidade","fonte","registro_id",
                "descricao","como_corrigir","impacto_estimado_RS","responsavel_sugerido","prazo_dias"
            ]),
            "citacoes": []
        }

    allf = pd.concat(rows, ignore_index=True)
    view = allf.head(500).copy()

    # --- tenta OpenAI, se existir chave
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)

            prompt_sistema = (
                "Você é um analista sênior de faturamento hospitalar (SUS + Privado). "
                "Priorize riscos de glosa, estime impacto financeiro, explique causa raiz "
                "e proponha ações corretivas objetivas. Responda em JSON."
            )
            prompt_usuario = {
                "meta": meta,
                "campos_df": list(view.columns),
                "amostra_achados": view.fillna("").to_dict(orient="records"),
                "formato_esperado": {
                    "resumo_md": "Markdown",
                    "acoes": [{
                        "prioridade":"P1|P2|P3","regra_id":"...","gravidade":"alta|media|baixa",
                        "fonte":"TISS|AIH|BPA|APAC","registro_id":"...","descricao":"...",
                        "como_corrigir":"...","impacto_estimado_RS":"num",
                        "responsavel_sugerido":"...","prazo_dias":"int"
                    }],
                    "citacoes": [{"tipo":"regra|contrato|tabela","referencia":"..."}]
                }
            }

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role":"system","content": prompt_sistema},
                    {"role":"user","content": json.dumps(prompt_usuario, ensure_ascii=False)}
                ],
                temperature=0.2
            )
            txt = resp.choices[0].message.content or ""
            payload = {}
            try:
                s, e = txt.find("{"), txt.rfind("}")
                if s != -1 and e != -1:
                    payload = json.loads(txt[s:e+1])
            except Exception:
                payload = {}

            acoes_df = pd.DataFrame(payload.get("acoes", [])) if payload.get("acoes") else pd.DataFrame(columns=[
                "prioridade","regra_id","gravidade","fonte","registro_id",
                "descricao","como_corrigir","impacto_estimado_RS","responsavel_sugerido","prazo_dias"
            ])
            return {
                "resumo_md": payload.get("resumo_md", txt if txt else "### Resumo Executivo (IA)\n\nSem texto."),
                "acoes": acoes_df,
                "citacoes": payload.get("citacoes", [])
            }
        except Exception:
            pass  # cai no determinístico

    # --- Fallback determinístico
    vc = view["regra_id"].value_counts().head(7).to_dict() if "regra_id" in view.columns else {}
    perdas = view["impacto_estimado_RS"].fillna(0).sum() if "impacto_estimado_RS" in view.columns else 0.0
    resumo = f"""### Resumo Executivo (Automático)
- Competência: **{meta.get('competencia','(não informada)')}**
- Top regras: **{vc}**
- Estimativa de impacto (somatório disponível): **R$ {perdas:,.2f}**
- Próximas ações:
1. Corrigir campos obrigatórios (CID/TUSS) nas guias pendentes.
2. Ajustar divergências financeiras (vl_total ≠ qtd × vl_unit).
3. Revisar compatibilidade clínica (CID ↔ procedimento) via SIGTAP/TUSS.
4. Anexar laudos obrigatórios e reprocessar.
5. Monitorar clean-claim e DSO no próximo ciclo.
"""
    acoes = []
    for i, (reg, count) in enumerate(vc.items(), start=1):
        acoes.append({
            "prioridade": "P1" if i <= 3 else "P2",
            "regra_id": reg,
            "gravidade": "alta" if i <= 3 else "media",
            "fonte": "",
            "registro_id": "",
            "descricao": f"Tratar {reg} (ocorrências: {count})",
            "como_corrigir": "Corrigir registros sinalizados e revalidar.",
            "impacto_estimado_RS": None,
            "responsavel_sugerido": "Faturamento",
            "prazo_dias": 5 if i <= 3 else 10
        })
    return {"resumo_md": resumo, "acoes": pd.DataFrame(acoes), "citacoes": []}


# =========================
# IA – SUS e Privado (insights estratégicos sem API)
# =========================
def _shape(df: Optional[pd.DataFrame]) -> int:
    return 0 if df is None else int(df.shape[0])

def ia_insights_sus(aps_df, sia_df, sih_df, cnes_prof_df, cnes_eqp_df, competencia: str):
    aps_total  = _shape(aps_df)
    sia_total  = _shape(sia_df)
    sih_total  = _shape(sih_df)
    n_cbo      = 0 if cnes_prof_df is None else cnes_prof_df.get("CBO", pd.Series(dtype=str)).nunique()
    n_eqp      = 0 if cnes_eqp_df is None else cnes_eqp_df.get("EQUIPAMENTO", pd.Series(dtype=str)).nunique()

    # Heurísticas de achados rápidos (variam conforme dados)
    achados = []
    if aps_df is not None and not aps_df.empty:
        if any(c.lower().startswith("diab") or "dm" in c.lower() for c in aps_df.columns):
            achados.append(("APS","Cobertura DM/HAS possivelmente baixa","Estratificação + busca ativa"))
        else:
            achados.append(("APS","Campos de APS genéricos — sugerir mapeamento","Padronizar layout SISAB"))
    if sia_df is not None and not sia_df.empty:
        achados.append(("SIA","Rastreios (citopatol./mama) possivelmente subutilizados","Ajustar agendas/encaminhamentos"))
    if sih_df is not None and not sih_df.empty:
        achados.append(("SIH","Internações sensíveis à APS podem estar elevadas","Fortalecer linhas de cuidado"))

    md = f"""### Resumo Executivo SUS – {competencia}
- **APS (SISAB)**: {aps_total} registros.
- **SIA-SUS (BPA/APAC)**: {sia_total} registros.
- **SIH-SUS (AIH)**: {sih_total} registros.
- **CNES**: {n_cbo} CBOs e {n_eqp} tipos de equipamentos.

#### Oportunidades (linha-mestra)
1. Checar aderência à **Portaria 1631/2015** (oferta/equip/leitos por perfil populacional).
2. Alinhar **produção APS** aos indicadores (pré-natal, citopatológico, HAS/DM, saúde bucal).
3. Cruzar **SIA x SIH** para subfinanciamento (alto custo sem contrapartida).
4. Mapear **gargalos** por CNES (RH/equipamentos) e rotas assistenciais.
5. Metas por unidade com foco em acesso e desfecho.
"""
    pts = pd.DataFrame([{"tema":t, "achado":a, "acao":ac, "impacto_RS":None} for (t,a,ac) in achados]) \
         if achados else pd.DataFrame(columns=["tema","achado","acao","impacto_RS"])
    return md, pts

def ia_insights_privado(tiss_df, contratos_df, competencia: str):
    total_guias = _shape(tiss_df)
    operadoras = [] if contratos_df is None or contratos_df.empty else \
        sorted(contratos_df.get("operadora", pd.Series(dtype=str)).dropna().unique().tolist())

    md = f"""### Resumo Executivo Privado – {competencia}
- **Guias TISS analisadas**: {total_guias}
- **Operadoras configuradas**: {', '.join(operadoras) if operadoras else 'não configurado'}

#### Linhas de ação prioritárias
1. **Clean-claim**: auditar CID/TUSS e anexos por operadora (reduzir glosa inicial).
2. **Preço & Pacotes**: checar teto/pacote vs custo real (SGH/DRG se aplicável).
3. **Recebíveis & DSO**: fila de reenvios/recursos com templates.
4. **Mix**: priorizar procedimentos de maior margem e destravar autorizações.
"""
    acoes = pd.DataFrame([
        {"prioridade":"P1","tema":"Clean-claim","acao":"Checklist pré-envio por operadora","impacto_RS":None,"prazo_dias":7},
        {"prioridade":"P1","tema":"Financeiro","acao":"Revisão de pacotes/tetos vs custo","impacto_RS":None,"prazo_dias":10},
        {"prioridade":"P2","tema":"DSO","acao":"Fila de recursos automatizada","impacto_RS":None,"prazo_dias":14},
    ])
    return md, acoes


# =========================
# Auditoria – validadores
# =========================
def validate_tiss_csv(df: pd.DataFrame, fonte_nome="TISS") -> pd.DataFrame:
    findings = []
    required_cols = ["numero_guia","cid10","tuss_codigo","qtd","vl_unit","vl_total"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        findings.append(dict(regra_id="TISS_CAMPOS_OBR", gravidade="alta",
                             registro_id="-", descricao=f"Colunas ausentes: {missing}",
                             como_corrigir="Adicionar colunas exigidas ao CSV antes da análise.",
                             impacto_estimado_RS=0))
        return pd.DataFrame(findings)

    for c in ["qtd","vl_unit","vl_total"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    for i, row in df.iterrows():
        rid = str(row.get("numero_guia", i))
        if pd.isna(row.get("cid10")) or str(row.get("cid10")).strip() == "":
            findings.append(dict(regra_id="TISS_CID_OBR", gravidade="alta", registro_id=rid,
                                 descricao="CID-10 ausente.",
                                 como_corrigir="Preencher CID-10 conforme laudo/diagnóstico.",
                                 impacto_estimado_RS=None))
        if pd.isna(row.get("tuss_codigo")) or str(row.get("tuss_codigo")).strip() == "":
            findings.append(dict(regra_id="TISS_TUSS_OBR", gravidade="alta", registro_id=rid,
                                 descricao="TUSS ausente.",
                                 como_corrigir="Preencher código TUSS vigente.",
                                 impacto_estimado_RS=None))
        if not (pd.isna(df.at[i,"qtd"]) or pd.isna(df.at[i,"vl_unit"]) or pd.isna(df.at[i,"vl_total"])):
            calc = df.at[i,"qtd"] * df.at[i,"vl_unit"]
            if abs(calc - df.at[i,"vl_total"]) > 0.01:
                findings.append(dict(regra_id="TISS_FINANCEIRO", gravidade="media", registro_id=rid,
                                     descricao=f"vl_total ({df.at[i,'vl_total']}) != qtd*vl_unit ({round2(calc)}).",
                                     como_corrigir="Ajustar quantidade/valor unitário ou total.",
                                     impacto_estimado_RS=abs(calc-df.at[i,"vl_total"])))
    return pd.DataFrame(findings)

def validate_fixed_lines(text: str, fonte_nome="FIXO") -> pd.DataFrame:
    lines = text.splitlines()
    if not lines:
        return pd.DataFrame([dict(regra_id="ARQ_VAZIO", gravidade="alta", registro_id="-",
                                  descricao="Arquivo sem linhas.",
                                  como_corrigir="Reexportar arquivo do sistema.",
                                  impacto_estimado_RS=0)])
    lens = [len(l.rstrip("\r\n")) for l in lines]
    mode_len = max(set(lens), key=lens.count)
    pct_diff = sum(1 for L in lens if L != mode_len) / len(lens) * 100

    findings = []
    if pct_diff > 5:
        findings.append(dict(regra_id="FIXO_COMPRIMENTO", gravidade="media", registro_id="-",
                             descricao=f"{pct_diff:.1f}% das linhas diferem do comprimento modal ({mode_len}).",
                             como_corrigir="Verificar layout/quebras de linha; reexportar.",
                             impacto_estimado_RS=0))

    has_codes = any(PROC_10D.search(l) for l in lines)
    has_jul25 = any(detect_jul_2025(l) for l in lines)
    if not has_codes:
        findings.append(dict(regra_id="SIGTAP_AUSENTE", gravidade="alta", registro_id="-",
                             descricao="Não foram encontrados códigos de 10 dígitos (SIGTAP).",
                             como_corrigir="Confirmar se o arquivo contém os procedimentos.",
                             impacto_estimado_RS=0))
    if not has_jul25:
        findings.append(dict(regra_id="COMPETENCIA_DUVIDA", gravidade="baixa", registro_id="-",
                             descricao="Não detectei datas de julho/2025 nas linhas.",
                             como_corrigir="Verificar competência do lote.",
                             impacto_estimado_RS=0))
    return pd.DataFrame(findings)


# =========================
# TABS
# =========================
tab1, tab2, tab3, tab4 = st.tabs(["🔎 Auditoria", "🏥 Painel SUS", "🏷️ Painel Privado", "📚 SIGTAP (Jul/2025)"])


# ---- TAB 1: Auditoria
with tab1:
    # state para IA
    if "findings_pack" not in st.session_state:
        st.session_state["findings_pack"] = []
    if "competencia_atual" not in st.session_state:
        st.session_state["competencia_atual"] = None

    with st.sidebar:
        st.header("Parâmetros da Auditoria")
        competencia = st.text_input("Competência (AAAAMM)", value="202507")
        n_files = st.number_input("Quantos arquivos você vai enviar?", min_value=1, max_value=10, value=1, step=1)

    uploaded = []
    for i in range(int(n_files)):
        col1, col2 = st.columns([3,2])
        with col1:
            f = st.file_uploader(f"Arquivo {i+1}", type=None, key=f"fu_{i}")
        with col2:
            tipo = st.selectbox("Tipo", ["AIH_fixo","BPA_fixo","APAC_fixo","TISS_CSV"], key=f"tipo_{i}")
        uploaded.append((f,tipo))

    if st.button("Rodar Auditoria", key="rodar_aud"):
        all_findings, det_rows = [], []
        for i, (f,tipo) in enumerate(uploaded, start=1):
            if not f:
                continue
            fname = f.name
            st.write(f"**Processando:** `{fname}` ({tipo})")

            if tipo == "TISS_CSV":
                try:
                    df = read_csv_smart(f)
                except Exception:
                    f.seek(0)
                    df = pd.read_excel(f)
                st.info("Esperado: numero_guia, cid10, tuss_codigo, qtd, vl_unit, vl_total ...")
                findings = validate_tiss_csv(df, "TISS")
                if not findings.empty:
                    st.dataframe(findings, use_container_width=True)
                all_findings.append(("TISS", findings))
                st.write("Prévia TISS (200 linhas):")
                st.dataframe(df.head(200).copy(), use_container_width=True)
            else:
                text = try_read_text(f)
                findings = validate_fixed_lines(text, tipo)
                if not findings.empty:
                    st.dataframe(findings, use_container_width=True)
                all_findings.append((tipo, findings))
                lines = text.splitlines()
                for idx, ln in enumerate(lines[:500], start=1):
                    codes = extract_codes(ln)
                    if codes:
                        det_rows.append(dict(arquivo=fname, line_idx=idx, codes_10d=";".join(codes), n_codes=len(codes)))

        # — Excel de saída
        xls_bytes = io.BytesIO()
        with pd.ExcelWriter(xls_bytes, engine="openpyxl") as writer:
            for fonte, df_f in all_findings:
                df_tmp = (df_f if df_f is not None and not df_f.empty
                          else pd.DataFrame(columns=["regra_id","gravidade","registro_id","descricao","como_corrigir","impacto_estimado_RS"]))
                df_tmp.to_excel(writer, sheet_name=f"{fonte}_erros", index=False)
            det_df = pd.DataFrame(det_rows) if det_rows else pd.DataFrame(columns=["arquivo","line_idx","codes_10d","n_codes"])
            det_df.to_excel(writer, sheet_name="Detalhe_codigos", index=False)
            resumo = []
            for fonte, df_f in all_findings:
                if df_f is None or df_f.empty:
                    continue
                top = df_f["regra_id"].value_counts().head(5).to_dict()
                resumo.append(dict(fonte=fonte, top5_regra_ids=str(top)))
            pd.DataFrame(resumo).to_excel(writer, sheet_name="Resumo_executivo", index=False)

        st.download_button("⬇️ Baixar Correcoes_Imediatas.xlsx",
                           data=xls_bytes.getvalue(),
                           file_name="Correcoes_Imediatas.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="dl_corr_imediatas")

        # salva no state para IA
        st.session_state["findings_pack"] = all_findings
        st.session_state["competencia_atual"] = competencia

        st.success("Auditoria concluída! Abaixo, opcional: Analisar com IA.")

    # — IA
    st.markdown("---")
    st.subheader("🧠 Analisar com IA")
    if not st.session_state["findings_pack"]:
        st.info("Rode a auditoria para habilitar a análise de IA.")
    else:
        meta = {"competencia": st.session_state["competencia_atual"]}
        if st.button("Gerar Resumo Executivo (IA)", key="ia_auditoria"):
            resultado = ia_priorizar_e_sugerir(st.session_state["findings_pack"], meta)
            st.markdown(resultado["resumo_md"])
            if resultado["acoes"] is not None and not resultado["acoes"].empty:
                st.write("**Plano de Ação Priorizado**")
                st.dataframe(resultado["acoes"], use_container_width=True)
                out_xls = io.BytesIO()
                with pd.ExcelWriter(out_xls, engine="openpyxl") as w:
                    resultado["acoes"].to_excel(w, sheet_name="Plano_de_Acao", index=False)
                    pd.DataFrame(resultado.get("citacoes", [])).to_excel(w, sheet_name="Citacoes", index=False)
                st.download_button("⬇️ Baixar Plano_de_Acao.xlsx",
                                   data=out_xls.getvalue(),
                                   file_name="Plano_de_Acao.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   key="dl_plano_acao_auditoria")
                md_bytes = io.BytesIO(resultado["resumo_md"].encode("utf-8"))
                st.download_button("⬇️ Baixar Resumo_IA.md",
                                   data=md_bytes.getvalue(),
                                   file_name="Resumo_IA.md",
                                   mime="text/markdown",
                                   key="dl_resumo_ia_auditoria")
            st.success("Análise de IA concluída.")


# ---- TAB 2: Painel SUS (Upload + IA)
with tab2:
    st.subheader("Painel SUS – Upload + IA (SISAB/SIA/SIH/CNES)")
    colA, colB = st.columns(2)
    with colA:
        aps_file = st.file_uploader("APS (SISAB) – CSV/Excel", type=["csv","xlsx"], key="aps")
        sia_file = st.file_uploader("SIA-SUS (BPA/APAC) – CSV/Excel", type=["csv","xlsx"], key="sia")
        sih_file = st.file_uploader("SIH-SUS (AIH) – CSV/Excel", type=["csv","xlsx"], key="sih")
    with colB:
        cnes_prof = st.file_uploader("CNES Profissionais – CSV/Excel", type=["csv","xlsx"], key="cnes_prof")
        cnes_eqp = st.file_uploader("CNES Equipamentos – CSV/Excel", type=["csv","xlsx"], key="cnes_eqp")
        competencia_sus = st.text_input("Competência (AAAAMM)", value="202507", key="comp_sus")

    def _read_any(f):
        if not f: 
            return None
        try:
            return read_csv_smart(f)
        except Exception:
            f.seek(0)
            return pd.read_excel(f)

    aps_df       = _read_any(aps_file)
    sia_df       = _read_any(sia_file)
    sih_df       = _read_any(sih_file)
    cnes_prof_df = _read_any(cnes_prof)
    cnes_eqp_df  = _read_any(cnes_eqp)

    if st.button("🧠 Analisar com IA (SUS)", key="ia_sus"):
        md, pts = ia_insights_sus(aps_df, sia_df, sih_df, cnes_prof_df, cnes_eqp_df, competencia_sus)
        st.markdown(md)
        if pts is not None and not pts.empty:
            st.dataframe(pts, use_container_width=True)
            out = io.BytesIO()
            with pd.ExcelWriter(out, engine="openpyxl") as w:
                pts.to_excel(w, sheet_name="Pontos_de_Atencao", index=False)
            st.download_button("⬇️ Baixar Pontos_de_Atencao_SUS.xlsx",
                               data=out.getvalue(),
                               file_name="Pontos_de_Atencao_SUS.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="dl_pts_sus")


# ---- TAB 3: Painel Privado (Upload + IA)
with tab3:
    st.subheader("Painel Privado – Upload + IA (TISS/TUSS + Contratos)")
    col1, col2 = st.columns(2)
    with col1:
        tiss_file = st.file_uploader("TISS (CSV/XLSX do XML)", type=["csv","xlsx"], key="tiss_upload")
    with col2:
        contratos_file = st.file_uploader("Parâmetros Contratuais – XLSX/CSV", type=["xlsx","csv"], key="contratos_upload")
    competencia_priv = st.text_input("Competência (AAAAMM)", value="202507", key="comp_priv")

    def _read_any2(f):
        if not f: return None
        try:
            return read_csv_smart(f)
        except Exception:
            f.seek(0); 
            return pd.read_excel(f)

    tiss_df = _read_any2(tiss_file)
    contratos_df = _read_any2(contratos_file)

    if st.button("🧠 Analisar com IA (Privado)", key="ia_privado"):
        md, acoes = ia_insights_privado(tiss_df, contratos_df, competencia_priv)
        st.markdown(md)
        if acoes is not None and not acoes.empty:
            st.dataframe(acoes, use_container_width=True)
            out = io.BytesIO()
            with pd.ExcelWriter(out, engine="openpyxl") as w:
                acoes.to_excel(w, sheet_name="Plano_de_Acao", index=False)
            st.download_button("⬇️ Baixar Plano_de_Acao_Privado.xlsx",
                               data=out.getvalue(),
                               file_name="Plano_de_Acao_Privado.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="dl_plano_priv")


# ---- TAB 4: SIGTAP (Jul/2025) — consolidação de códigos 10d
with tab4:
    st.subheader("Consolidação SIGTAP – Jul/2025 (AIH/BPA/APAC linha fixa)")
    st.write("Envie AIH/BPA/APAC (texto/linha fixa). O app extrai códigos de 10 dígitos, sinaliza Jul/2025 e gera planilha para cruzar com SIGTAP.")

    aih = st.file_uploader("AIH (linha fixa)", type=None, key="sig_aih")
    bpa = st.file_uploader("BPA (linha fixa)", type=None, key="sig_bpa")
    apac = st.file_uploader("APAC (linha fixa)", type=None, key="sig_apac")

    if st.button("Gerar Excel SIGTAP (Jul/2025)", key="sig_jul"):
        def process(file, fonte):
            if not file: return []
            text = try_read_text(file)
            rows = []
            for idx, ln in enumerate(text.splitlines(), start=1):
                codes = extract_codes(ln)
                jul = detect_jul_2025(ln)
                if codes:
                    rows.append([fonte, idx, ";".join(codes), len(codes), int(jul)])
            return rows

        rows = []
        rows += process(aih, "AIH")
        rows += process(bpa, "BPA")
        rows += process(apac, "APAC")

        df_det = pd.DataFrame(rows, columns=["fonte","line_idx","codes_10d","n_codes","has_julho_2025"])

        # agregação simples por código/fonte
        agg = []
        if not df_det.empty:
            for fonte, g in df_det.groupby("fonte"):
                exploded = g.assign(code=g["codes_10d"].str.split(";")).explode("code")
                for code, g2 in exploded.groupby("code"):
                    agg.append([code, fonte, g2.shape[0], g2["has_julho_2025"].sum()])
        df_agg = pd.DataFrame(agg, columns=["codigo","fonte","qtd","qtd_julho"])

        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            # Resumo
            resumo = pd.DataFrame({
                "Arquivos_lidos":[
                    f"AIH: {'OK' if aih else 'não enviado'}",
                    f"BPA: {'OK' if bpa else 'não enviado'}",
                    f"APAC: {'OK' if apac else 'não enviado'}",
                    "Cole SIGTAP vigente em 'SIGTAP_importe' (AAAAMM=202507)."
                ]
            })
            resumo.to_excel(w, sheet_name="Resumo", index=False)

            # SIGTAP_importe (vazia para colar tabela oficial)
            pd.DataFrame(columns=["CO_PROCEDIMENTO","NO_PROCEDIMENTO","VL_SH","VL_SA","VL_OPM","VL_TOTAL_SUGERIDO","COMPETENCIA"])\
              .to_excel(w, sheet_name="SIGTAP_importe", index=False)

            # Consolidado_proc
            if df_agg.empty:
                df_base = pd.DataFrame(columns=["codigo","desc_sigtap","vl_sh","vl_sa","vl_opm","vl_unit_total","qtd_total","qtd_julho","valor_total_estimado"])
            else:
                df_base = (df_agg.pivot_table(index="codigo", columns="fonte", values="qtd", aggfunc="sum", fill_value=0)
                           .assign(qtd_total=lambda d: d.sum(axis=1))
                           .assign(qtd_julho=0)
                           .reset_index()[["codigo","qtd_total","qtd_julho"]])
            df_base.assign(desc_sigtap="", vl_sh=0, vl_sa=0, vl_opm=0, vl_unit_total=0, valor_total_estimado=0)\
                   .to_excel(w, sheet_name="Consolidado_proc", index=False)

        st.success("Planilha gerada!")
        st.download_button("⬇️ Baixar consolidacao_SIGTAP_julho2025.xlsx",
                           data=out.getvalue(),
                           file_name="consolidacao_SIGTAP_julho2025.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="dl_sig_jul")

