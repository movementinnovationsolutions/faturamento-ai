import io, re
import pandas as pd
from datetime import datetime
import streamlit as st
import json, os

# =========================
# Helpers gerais
# =========================
PROC_10D = re.compile(r"(?<!\\d)(\\d{10})(?!\\d)")
DATE_8D = re.compile(r"\\b(\\d{8})\\b")

def try_read_text(file):
    try:
        return file.getvalue().decode("latin-1", errors="ignore")
    except Exception:
        file.seek(0)
        return file.getvalue().decode("utf-8", errors="ignore")

def detect_jul_2025(text):
    for token in DATE_8D.findall(text):
        for fmt in ("%d%m%Y", "%Y%m%d"):
            try:
                d = datetime.strptime(token, fmt)
                if d.year == 2025 and d.month == 7:
                    return True
            except Exception:
                continue
    return False

def extract_codes(text):
    return PROC_10D.findall(text)

def round2(x):
    try:
        return round(float(x), 2)
    except:
        return None

# =========================
# Auditoria – validadores
# =========================
def validate_tiss_csv(df, fonte_nome="TISS"):
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

def validate_fixed_lines(text, fonte_nome="FIXO"):
    lines = text.splitlines()
    if not lines:
        return pd.DataFrame([dict(regra_id="ARQ_VAZIO", gravidade="alta", registro_id="-",
                                  descricao="Arquivo sem linhas.",
                                  como_corrigir="Reexportar arquivo do sistema.",
                                  impacto_estimado_RS=0)])
    lens = [len(l.rstrip("\\r\\n")) for l in lines]
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
# UI principal
# =========================
st.set_page_config(page_title="Auditoria & Painéis (SUS + Privado)", layout="wide")
st.title("🧠 Auditoria & Painéis — SUS (AIH/BPA/APAC) + Privado (TISS/TUSS)")
st.caption("Movement Innovation Solutions")

tab1, tab2, tab3, tab4 = st.tabs(["🔎 Auditoria", "🏥 Painel SUS", "🏷️ Painel Privado", "📚 SIGTAP (Jul/2025)"])

# ---- TAB 1: Auditoria
with tab1:
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

    if st.button("Rodar Auditoria"):
        all_findings, det_rows = [], []
        for i, (f,tipo) in enumerate(uploaded, start=1):
            if not f: 
                continue
            fname = f.name
            st.write(f"**Processando:** `{fname}` ({tipo})")

            if tipo == "TISS_CSV":
                try:
                    df = pd.read_csv(f)
                except Exception:
                    f.seek(0)
                    df = pd.read_excel(f)
                st.info("Esperado: numero_guia, cid10, tuss_codigo, qtd, vl_unit, vl_total ...")
                findings = validate_tiss_csv(df, "TISS")
                if not findings.empty:
                    st.dataframe(findings)
                all_findings.append(("TISS", findings))
                st.write("Prévia TISS (200 linhas):")
                st.dataframe(df.head(200).copy())
            else:
                text = try_read_text(f)
                findings = validate_fixed_lines(text, tipo)
                if not findings.empty:
                    st.dataframe(findings)
