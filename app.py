import streamlit as st
import fitz  # PyMuPDF
import json
import io
import re
from groq import Groq

st.set_page_config(page_title="C525 Smart OFP Assistant", layout="wide", page_icon="✈️")

# =====================================================================
# SECTION 1 : EXTRACTION ET FORMATAGE
# =====================================================================

def extract_text_from_fp(fp_file):
    doc_fp = fitz.open(stream=fp_file.read(), filetype="pdf")
    total_pages = len(doc_fp)
    
    pages_to_read = list(range(min(15, total_pages)))
    if total_pages > 30:
        pages_to_read += list(range(total_pages - 15, total_pages))
        
    fp_text = " ".join([doc_fp[i].get_text() for i in set(pages_to_read)])
    doc_fp.close()
    return " ".join(fp_text.split())

def format_mass(val):
    """Arrondit une masse à l'entier supérieur/inférieur le plus proche sans décimales."""
    if val is None:
        return ""
    val_str = str(val).replace(" ", "").replace(",", ".")
    match = re.search(r"[-+]?\d*\.?\d+", val_str)
    if match:
        try:
            return str(int(round(float(match.group()))))
        except ValueError:
            return val_str
    return val_str

# =====================================================================
# SECTION 2 : ANALYSE INTELLIGENTE VIA GROQ (CHAIN OF THOUGHT)
# =====================================================================

def extract_data_with_ai(fp_text, wb_text, api_key):
    client = Groq(api_key=api_key)
    
    try:
        models_data = client.models.list().data
        active_models = [m.id for m in models_data if "whisper" not in m.id.lower() and "guard" not in m.id.lower()]
        
        if not active_models:
            st.error("❌ Aucun modèle disponible sur ce compte Groq.")
            return None

        selected_model = active_models[0]
        for m_id in active_models:
            if "llama-3" in m_id.lower():
                selected_model = m_id
                break
                
        prompt = f"""
        Tu es un dispatcher aéronautique expert. Tu vas analyser un Flight Package (Météo + APG) et un Weight & Balance (Loadsheet).
        
        MÉTHODE OBLIGATOIRE (Étape par étape) :
        1. DÉPART : Analyse le METAR du départ. Note le vent et la température (OAT). Détermine si la piste est DRY ou WET (pluie).
        2. PERFOS DÉPART (APG) : Va dans le tableau APG correspondant. Trouve la ligne exacte de la température (OAT).
           - Extrais le "T/O Power" (ex: 98.6).
           - Extrais la masse limite exacte. Indique si elle est limitée par les obstacles (OBST) ou la piste/structure (ST).
           - Extrais le Level Off (LVL OFF).
        3. MASSES (W&B) : Extrais les valeurs en NOMBRES ENTIERS (SANS DÉCIMALES, ex: 10382 et non 10381.83) lues sur le Weight & Balance :
           - tow : Take-Off Weight réel
           - zfw : Zero Fuel Weight réel
           - landing_weight : Landing Weight réel
        4. ARRIVÉE (DEST/ALTN) : Regarde les METAR/TAF pour la Destination et le Dégagement. Détermine si les pistes seront DRY ou WET. Va dans l'APG Landing et déduis le Max Landing Weight autorisé. Formatte en "DEST/ALTN" (ex: 9900/9900).

        DOCUMENTS FOURNIS :
        --- FLIGHT PACKAGE & APG ---
        {fp_text[:8000]} 
        
        --- WEIGHT & BALANCE ---
        {wb_text[:2000]}
        
        RÉPONSE ATTENDUE : Un objet JSON STRICT sans texte autour.
        {{
            "reasoning": "Ton analyse étape par étape",
            "tow": "Valeur entière exacte du WB",
            "zfw": "Valeur entière exacte du WB",
            "landing_weight": "Valeur entière exacte du WB",
            "to_power": "Puissance déduite de l'APG selon l'OAT",
            "obst_or_st_limiting": "OBST ou ST",
            "limiting_weight": "Masse limite déduite",
            "lvl_off": "Valeur APG",
            "escape_route": "Texte de la special departure procedure (si présente)",
            "max_ldg": "Valeur DEST/ALTN (ex: 9900/9900)"
        }}
        """
        
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=selected_model,
            temperature=0,
            response_format={"type": "json_object"}
        )
        
        response_text = chat_completion.choices[0].message.content
        return json.loads(response_text)
        
    except Exception as e:
        st.error(f"Erreur lors de l'analyse IA (Groq) : {str(e)}")
        return None

# =====================================================================
# SECTION 3 : GÉNÉRATION DU PDF FINAL
# =====================================================================

def draw_on_pdf(template_bytes, ai_data, user_params):
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    red_color = (1, 0, 0)
    
    # --- PAGE 1 : EN-TÊTE ET PERFORMANCES ---
    p1 = doc[0]
    
    # Formatage strict des masses en entiers
    tow_clean = format_mass(ai_data.get("tow", ""))
    zfw_clean = format_mass(ai_data.get("zfw", ""))
    ldg_clean = format_mass(ai_data.get("landing_weight", ""))
    
    def write_next_to(keyword, text, offset_x=5, offset_y=0, font="hebo", size=10, color=(0, 0, 0)):
        if not text:
            return
        rects = p1.search_for(keyword)
        if rects:
            r = rects[0]
            p1.insert_text((r.x1 + offset_x, r.y1 + offset_y), str(text), fontsize=size, fontname=font, color=color)

    # 1. Écriture des données chiffrées (RUNWAY reste délibérément vide)
    write_next_to("T/O POWER:", ai_data.get("to_power", ""))
    write_next_to("TO WEIGHT:", tow_clean)
    write_next_to("LVL OFF:", ai_data.get("lvl_off", ""))
    if zfw_clean:
        write_next_to("RMQ:", f"ZFW: {zfw_clean}")

    # 2. Gestion de OBST / ST LIMIT (Barrer l'option qui n'est pas la limite)
    limit_titles = p1.search_for("OBST/ST LIMIT:")
    if limit_titles:
        r_lim = limit_titles[0]
        p1.insert_text((r_lim.x1 + 5, r_lim.y1), str(ai_data.get("limiting_weight", "")), fontsize=10, fontname="hebo")
        
        limiting_type = str(ai_data.get("obst_or_st_limiting", "")).upper()
        # On localise "OBST" et "ST" dans l'intitulé lui-même
        line_rect = fitz.Rect(r_lim.x0, r_lim.y0 - 2, r_lim.x1, r_lim.y1 + 2)
        obst_hits = p1.search_for("OBST", clip=line_rect)
        st_hits = p1.search_for("ST", clip=line_rect)
        
        if "ST" in limiting_type and obst_hits:
            ox = obst_hits[0]
            y_mid = (ox.y0 + ox.y1) / 2
            p1.draw_line((ox.x0, y_mid), (ox.x1, y_mid), color=red_color, width=1.5)
        elif "OBST" in limiting_type and st_hits:
            # On prend la première occurrence de ST
            sx = st_hits[0]
            y_mid = (sx.y0 + sx.y1) / 2
            p1.draw_line((sx.x0, y_mid), (sx.x1, y_mid), color=red_color, width=1.5)

    # 3. Alignement propre dans le cadre d'atterrissage (Page 1 en bas à droite)
    update_rects = p1.search_for("UPDATE:")
    if update_rects:
        r_upd = update_rects[0]
        # Position X alignée pour les deux valeurs
        align_x = r_upd.x1 + 8
        if ldg_clean:
            # Ligne supérieure : au-dessus ou au niveau de UPDATE
            p1.insert_text((align_x, r_upd.y0 - 8), f"WB LDG WT: {ldg_clean}", fontsize=9, fontname="hebo")
        if ai_data.get("max_ldg"):
            # Ligne inférieure : alignée verticalement sur UPDATE
            p1.insert_text((align_x, r_upd.y1), f"MAX: {ai_data.get('max_ldg')}", fontsize=9, fontname="hebo")

    # 4. Escape Route
    if ai_data.get("escape_route"):
        esc_rects = p1.search_for("1E0 ESCAPE PROCEDURE:") or p1.search_for("1EO ESCAPE PROCEDURE:")
        if esc_rects:
            r_esc = esc_rects[0]
            box = fitz.Rect(r_esc.x0, r_esc.y1 + 4, r_esc.x0 + 350, r_esc.y1 + 75)
            p1.insert_textbox(box, ai_data["escape_route"], fontsize=8, fontname="helv", align=0)

    # 5. Type d'Ops (recherche limitée à l'en-tête uniquement)
    ops_titles = p1.search_for("TYPE OF OPS:")
    if ops_titles:
        r_ops = ops_titles[0]
        scan_box = fitz.Rect(r_ops.x1, r_ops.y0 - 4, r_ops.x1 + 180, r_ops.y1 + 4)
        found_ops = p1.search_for(user_params["ops"], clip=scan_box)
        if found_ops:
            op_r = found_ops[0]
            p1.draw_rect(fitz.Rect(op_r.x0 - 2, op_r.y0 - 2, op_r.x1 + 2, op_r.y1 + 2), color=red_color, width=1.5)

    # 6. Entourage PF / PM (Positionnés sur CPT et F/O dans l'en-tête)
    # Sur l'OFP PPS, CPT est vers y=147 et F/O vers y=158
    all_pf_pm = sorted(p1.search_for("PF-PM"), key=lambda r: r.y0)
    crew_pf_pm = [r for r in all_pf_pm if 100 < r.y0 < 300]
    
    if len(crew_pf_pm) >= 2:
        cpt_box = crew_pf_pm[0]
        fo_box = crew_pf_pm[1]
        
        def circle_role(box_rect, role):
            w = box_rect.x1 - box_rect.x0
            mid_x = box_rect.x0 + (w / 2)
            if role == "PF":
                target = fitz.Rect(box_rect.x0 - 2, box_rect.y0 - 2, mid_x - 1, box_rect.y1 + 2)
            else:
                target = fitz.Rect(mid_x + 1, box_rect.y0 - 2, box_rect.x1 + 2, box_rect.y1 + 2)
            p1.draw_rect(target, color=red_color, width=1.5)
            
        if user_params["pf"] == "CPT":
            circle_role(cpt_box, "PF")
            circle_role(fo_box, "PM")
        else:
            circle_role(cpt_box, "PM")
            circle_role(fo_box, "PF")

    # --- PAGE 2 : ROUTE & MORA ---
    if len(doc) > 1:
        p2 = doc[1]
        
        if user_params.get("driftdown_fl"):
            toc_rects = p2.search_for("-TOC-")
            if toc_rects:
                r_toc = toc_rects[0]
                p2.insert_text((r_toc.x1 + 15, r_toc.y1), f"DD: FL {user_params['driftdown_fl']}", fontsize=10, fontname="hebo", color=red_color)

        words = p2.get_text("words")
        max_mora, max_mora_rect = 0, None
        for w in words:
            text = w[4]
            if text.isdigit() and 50 <= int(text) <= 250:
                val = int(text)
                if val > max_mora:
                    max_mora = val
                    max_mora_rect = fitz.Rect(w[:4])
                    
        if max_mora_rect:
            rec_alt = (max_mora * 100) + 1000
            p2.insert_text((max_mora_rect.x1 + 15, max_mora_rect.y1), f"Rec: {rec_alt} FT", fontsize=10, fontname="hebo", color=red_color)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

# =====================================================================
# SECTION 4 : INTERFACE UTILISATEUR (STREAMLIT)
# =====================================================================

st.markdown("### 🛫 Documents de vol sources")
col1, col2, col3 = st.columns(3)
with col1: 
    fp_file = st.file_uploader("1. Flight Package (PDF)", type="pdf")
with col2: 
    wb_file = st.file_uploader("2. Weight & Balance (PDF)", type="pdf")
with col3: 
    template_file = st.file_uploader("3. OFP PPS (PDF)", type="pdf")

st.markdown("### ⚙️ Paramètres du vol")
col_pf, col_ops, col_dd = st.columns(3)
with col_pf: 
    pf = st.radio("Pilot Flying (PF) :", ["CPT", "FO"], horizontal=True)
with col_ops: 
    ops = st.radio("Type of Ops :", ["COM", "PVT", "TRG", "MED"], horizontal=True)
with col_dd: 
    driftdown_fl = st.text_input("Drift Down FL :", placeholder="ex: 220")

if st.button("🚀 Analyser avec l'IA & Compléter l'OFP", type="primary", use_container_width=True):
    
    try:
        api_key = st.secrets["GROQ_API_KEY"]
    except Exception:
        st.error("⚠️ Clé GROQ_API_KEY absente des Secrets Streamlit.")
        st.stop()

    if not (fp_file and wb_file and template_file):
        st.warning("⚠️ Veuillez charger les 3 documents PDF avant de lancer l'analyse.")
    else:
        with st.spinner("Analyse météo, déduction APG et tracé en cours..."):
            doc_wb = fitz.open(stream=wb_file.read(), filetype="pdf")
            wb_text = " ".join([page.get_text() for page in doc_wb])
            wb_text = " ".join(wb_text.split())
            doc_wb.close()
            
            fp_text = extract_text_from_fp(fp_file)

            ai_data = extract_data_with_ai(fp_text, wb_text, api_key)

            if ai_data:
                st.success("✅ Données analysées avec succès.")
                
                with st.expander("🔍 Voir le raisonnement de l'IA et les données brutes"):
                    st.json(ai_data)
                
                user_params = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
                template_bytes = template_file.read()
                final_pdf = draw_on_pdf(template_bytes, ai_data, user_params)

                st.download_button(
                    label="📥 Télécharger l'OFP Complété",
                    data=final_pdf,
                    file_name="OFP_Smart_Complete.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )
