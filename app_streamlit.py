import io, re
import pandas as pd
from datetime import datetime
import streamlit as st

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
                all_findings.append((tipo, findings))
                lines = text.splitlines()
                for idx, ln in enumerate(lines[:500], start=1):
                    codes = extract_codes(ln)
                    if codes:
                        det_rows.append(dict(arquivo=fname, line_idx=idx, codes_10d=";".join(codes), n_codes=len(codes)))

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

        st.success("Auditoria concluída!")
        st.download_button("⬇️ Baixar Correcoes_Imediatas.xlsx",
                           data=xls_bytes.getvalue(),
                           file_name="Correcoes_Imediatas.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---- TAB 2: Painel SUS
with tab2:
    st.subheader("Painel de Risco de Glosa SUS – Template")
    st.write("Gera um Excel com Indicadores, Matriz de Risco e Dashboard. Você pode editar metas e colar produção (BPA/APAC/AIH).")

    default_indic = pd.DataFrame({
        "Indicador": [
            "Pré-natal (6 cons. até 12ª sem.)",
            "Pré-natal (testes rápidos gestantes)",
            "Saúde Bucal (gestantes)",
            "Citopatológico",
            "Hipertensão acompanhada",
            "Diabetes acompanhada",
            "Vacinação (Pólio/Penta)"
        ],
        "Valor(%)": [57, 44, 23, 20, 19, 6, 92],
        "Meta(%)":  [70, 60, 40, 40, 50, 40, 90]
    })
    st.dataframe(default_indic, use_container_width=True)

    if st.button("Gerar Excel SUS"):
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            default_indic.to_excel(w, sheet_name="Indicadores SUS", index=False)
            # Matriz de risco
            df2 = default_indic.copy()
            def risco(row):
                if row["Valor(%)"] >= row["Meta(%)"]: return "Verde (Seguro)"
                if row["Valor(%)"] >= 0.7*row["Meta(%)"]: return "Amarelo (Atenção)"
                return "Vermelho (Crítico)"
            df2["Risco"] = df2.apply(risco, axis=1)
            df2.to_excel(w, sheet_name="Matriz de Risco", index=False)
            # Importar Produção (vazia)
            pd.DataFrame({"Cole_aqui":"BPA/APAC/AIH"}).to_excel(w, sheet_name="Importar Producao", index=False)
            # Dashboard (texto)
            dash = pd.DataFrame({"Resumo":[
                "✅ Vacinação em nível seguro (92%).",
                "⚠️ Pré-natal, Saúde Bucal, Citopatológico, Hipertensão e Diabetes com risco de perda.",
                "🔴 Ponto mais crítico: Diabetes acompanhada (6% vs meta 40%)."
            ]})
            dash.to_excel(w, sheet_name="Dashboard", index=False)
        st.download_button("⬇️ Baixar painel_risco_glosa_SUS.xlsx",
                           data=out.getvalue(),
                           file_name="painel_risco_glosa_SUS.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---- TAB 3: Painel Privado
with tab3:
    st.subheader("Painel Privado – TISS/TUSS (Template)")
    st.write("Gera um Excel com abas: Importar TISS, Validações, Matriz_Risco, Crosswalk_TUSS_CID, Param_Contratos e Dashboard.")

    if st.button("Gerar Excel Privado"):
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            cols = ["lote_id","operadora","tipo_guia","numero_guia","data_atendimento",
                    "beneficiario","matricula","prestador_cnes","executante_cpf_cbo",
                    "cid10","tuss_codigo","tuss_descricao","qtd","vl_unit","vl_total",
                    "anexos_laudos","status_envio","motivo_glosa_ret"]
            pd.DataFrame(columns=cols).to_excel(w, sheet_name="Importar TISS", index=False)

            valid = pd.DataFrame([
                ["<preencher>","CAMPOS","CID-10 obrigatório para Internação","A verificar","Alta","Inserir CID-10 compatível"],
                ["<preencher>","TUSS","TUSS válido na tabela vigente","A verificar","Alta","Verificar código TUSS"],
                ["<preencher>","COMPAT","CID compatível com TUSS","A verificar","Alta","Revisar nexo clínico"],
                ["<preencher>","ANEXOS","Laudo exigido para procedimento","A verificar","Média","Anexar laudo"],
                ["<preencher>","FINANCEIRO","vl_total = qtd*vl_unit","A verificar","Baixa","Corrigir quantidade/valor"]
            ], columns=["numero_guia","regra","descricao_regra","resultado","gravidade","acao_sugerida"])
            valid.to_excel(w, sheet_name="Validacoes", index=False)

            risco = pd.DataFrame([
                ["Operadora A", 88, 14, 52, "", "CID x TUSS"],
                ["Operadora B", 93, 9, 35, "", "Anexo faltante"],
                ["Operadora C", 96, 6, 28, "", "Erro financeiro"],
            ], columns=["operadora","clean_claim_rate(%)","glosa_inicial(%)","DSO(dias)","risco","principal_causa"])
            risco.to_excel(w, sheet_name="Matriz_Risco", index=False)

            cx = pd.DataFrame([["40.05.01.012","Exemplo – Procedimento","G56.0; G56.1; G57.0","Exemplo; substituir pela tabela oficial/contratual."]],
                              columns=["tuss_codigo","tuss_descricao","cid10_sugeridos","observacoes"])
            cx.to_excel(w, sheet_name="Crosswalk_TUSS_CID", index=False)

            contratos = pd.DataFrame([
                ["Operadora A","SP/SADT","N",1500,"",30,"—"],
                ["Operadora B","Internacao","S-DRG","","",45,"Pacote inclui honorário."]
            ], columns=["operadora","linha","pacote_DRG","teto_evento(R$)","coparticipacao(%)","prazo_pagto(dias)","observacao"])
            contratos.to_excel(w, sheet_name="Param_Contratos", index=False)

            dash = pd.DataFrame({"Como_usar":[
                "1) Cole o CSV do XML na aba 'Importar TISS'.",
                "2) Use 'Validacoes' para marcar regras atendidas/pendentes.",
                "3) Preencha 'Param_Contratos' por operadora.",
                "4) Consolide métricas na 'Matriz_Risco'."
            ]})
            dash.to_excel(w, sheet_name="Dashboard", index=False)

        st.download_button("⬇️ Baixar painel_privado_TISS_TUSS_template.xlsx",
                           data=out.getvalue(),
                           file_name="painel_privado_TISS_TUSS_template.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---- TAB 4: SIGTAP
with tab4:
    st.subheader("Consolidação SIGTAP – Jul/2025")
    st.write("Envie arquivos AIH/BPA/APAC (texto/linha fixa). O app extrai códigos de 10 dígitos, sinaliza Jul/2025 e gera planilha para cruzar com SIGTAP.")

    aih = st.file_uploader("AIH (linha fixa)", type=None, key="sig_aih")
    bpa = st.file_uploader("BPA (linha fixa)", type=None, key="sig_bpa")
    apac = st.file_uploader("APAC (linha fixa)", type=None, key="sig_apac")

    if st.button("Gerar Excel SIGTAP (Jul/2025)"):
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
        rows += process(bpa, "BPA_oftalmo")
        rows += process(apac, "APAC_cirurgia")

        df_det = pd.DataFrame(rows, columns=["fonte","line_idx","codes_10d","n_codes","has_julho_2025"])
        # agregação simples por código/fonte
        agg = []
        if not df_det.empty:
            for fonte, g in df_det.groupby("fonte"):
                for code, g2 in g.assign(code=g["codes_10d"].str.split(";")).explode("code").groupby("code"):
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
            pd.DataFrame(columns=["CO_PROCEDIMENTO","NO_PROCEDIMENTO","VL_SH","VL_SA","VL_OPM","VL_TOTAL_SUGERIDO","COMPETENCIA"]).to_excel(
                w, sheet_name="SIGTAP_importe", index=False
            )

            # Consolidado_proc (com fórmulas para buscar descrição/valores ao colar a SIGTAP)
            if df_agg.empty:
                df_base = pd.DataFrame(columns=["codigo","desc_sigtap","vl_sh","vl_sa","vl_opm","vl_unit_total","qtd_total","qtd_julho","valor_total_estimado"])
            else:
                df_base = (df_agg.pivot_table(index="codigo", columns="fonte", values="qtd", aggfunc="sum", fill_value=0)
                                .assign(qtd_total=lambda d: d.sum(axis=1))
                                .assign(qtd_julho=0)
                                .reset_index()[["codigo","qtd_total","qtd_julho"]])
            # cria planilha e depois injeta fórmulas por coluna (Excel fará o VLOOKUP)
            df_base.assign(desc_sigtap="", vl_sh=0, vl_sa=0, vl_opm=0, vl_unit_total=0, valor_total_estimado=0)\
                   .to_excel(w, sheet_name="Consolidado_proc", index=False)
        # OBS: deixamos o usuário colar a SIGTAP e então fazer os PROCs/VLOOKUP no Excel.

        st.success("Planilha gerada!")
        st.download_button("⬇️ Baixar consolidacao_SIGTAP_julho2025.xlsx",
                           data=out.getvalue(),
                           file_name="consolidacao_SIGTAP_julho2025.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
