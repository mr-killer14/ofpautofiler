import streamlit as st
import fitz  # PyMuPDF
import json
import io
from groq import Groq

st.set_page_config(page_title="C525 Smart OFP Assistant", layout="wide", page_icon="✈️")

# =====================================================================
# SECTION 1 : EXTRACTION ET LECTURE DES DONNÉES (PDF -> TEXTE)
# =====================================================================

def extract_text_from_fp(fp_file):
    doc_fp = fitz.open(stream=fp_file.read(), filetype="pdf")
    total_pages = len(doc_fp)
    
    pages_to_read = list(range(min(15, total_pages)))
    if total_pages > 30:
        pages_to_read += list(range(total_pages - 15, total_pages))
        
    fp_text = " ".join([doc_fp[i].get_text() for i in set(pages_to_read)])
    doc_fp.close()
    
    fp_text = " ".join(fp_text.split())
    return fp_text

# =====================================================================
# SECTION 2 : ANALYSE INTELLIGENTE VIA GROQ (CHAIN OF THOUGHT)
# =====================================================================

def extract_data_with_ai(fp_text, wb_text, api_key):
    client = Groq(api_key=api_key)
    
    try:
        # Récupération dynamique du modèle
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
                
        # Le Prompt intègre maintenant une logique de "Reasoning" stricte
        prompt = f"""
        Tu es un dispatcher aéronautique expert. Tu vas analyser un Flight Package (Météo + APG) et un Weight & Balance (Loadsheet).
        
        MÉTHODE OBLIGATOIRE (Étape par étape) :
        1. DÉPART : Analyse le METAR du départ. Note le vent et la température (OAT). Détermine si la piste est DRY ou WET (pluie). Choisis la meilleure piste face au vent.
        2. PERFOS DÉPART (APG) : Va dans le tableau APG de cette piste et état (DRY/WET). Trouve la ligne exacte de la température (OAT).
           - Extrais le "T/O Power" (ex: 98.6).
           - Extrais la masse limite. Indique si elle est limitée par les obstacles (OBST) ou la piste/structure (ST).
           - Extrais le Level Off (LVL OFF).
        3. MASSES (W&B) : Extrais les valeurs EXACTES lues sur le document Weight & Balance : TOW (Take-Off Weight), ZFW (Zero Fuel Weight) et Landing Weight.
        4. ARRIVÉE (DEST/ALTN) : Regarde les METAR/TAF pour la Destination et le Dégagement. Détermine si les pistes seront DRY ou WET. Va dans l'APG Landing et déduis le Max Landing Weight autorisé. Formatte en "DEST/ALTN" (ex: 9900/9900).

        DOCUMENTS FOURNIS :
        --- FLIGHT PACKAGE & APG ---
        {fp_text[:8000]} 
        
        --- WEIGHT & BALANCE ---
        {wb_text[:2000]}
        
        RÉPONSE ATTENDUE : Un objet JSON STRICT. Le champ "reasoning" doit contenir ton analyse.
        {{
            "reasoning": "Ton analyse complète (METAR, Pistes, OAT, DRY/WET...)",
            "tow": "Valeur exacte du W&B",
            "zfw": "Valeur exacte du W&B",
            "landing_weight": "Valeur exacte du W&B",
            "to_power": "Puissance déduite de l'APG selon l'OAT",
            "obst_or_st_limiting": "OBST ou ST (lequel te limite ?)",
            "limiting_weight": "Valeur de la masse limite déduite",
            "lvl_off": "Valeur APG",
            "escape_route": "Texte de la special departure procedure (si applicable)",
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
# SECTION 3 : OUTILS DE DESSIN PAR ANCRAGE SPATIAL (GÉOMÉTRIE)
# =====================================================================

def draw_text_next_to(page, keyword, text, offset_x=5, offset_y=0, font="hebo", size=10, color=(0,0,0)):
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0]
        page.insert_text((r.x1 + offset_x, r.y1 + offset_y), str(text), fontsize=size, fontname=font, color=color)

def draw_text_above(page, keyword, text, offset_x=0, offset_y=-15, font="hebo", size=10, color=(0,0,0)):
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0]
        page.insert_text((r.x0 + offset_x, r.y0 + offset_y), str(text), fontsize=size, fontname=font, color=color)

# =====================================================================
# SECTION 4 : GÉNÉRATION DU PDF FINAL
# =====================================================================

def draw_on_pdf(template_bytes, ai_data, user_params):
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    red_color = (1, 0, 0)
    
    # --- PAGE 1 : EN-TÊTE ET PERFORMANCES ---
    p1 = doc[0]
    
    # 1. Textes classiques
    draw_text_next_to(p1, "T/O POWER:", ai_data.get("to_power", ""))
    draw_text_next_to(p1, "TO WEIGHT:", ai_data.get("tow", ""))
    draw_text_next_to(p1, "LVL OFF:", ai_data.get("lvl_off", ""))
    
    if ai_data.get("zfw"):
        draw_text_next_to(p1, "RMQ:", f"ZFW: {ai_data.get('zfw')}")
        
    if ai_data.get("landing_weight"):
        draw_text_above(p1, "UPDATE:", f"WB: Landing Weight {ai_data.get('landing_weight')}")

    # 2. Gestion pointue du OBST / ST LIMIT (Barrer celui qui ne sert pas)
    limit_titles = p1.search_for("OBST/ST LIMIT")
    if limit_titles:
        r_lim = limit_titles[0]
        # On écrit la masse limite
        p1.insert_text((r_lim.x1 + 5, r_lim.y1), str(ai_data.get("limiting_weight", "")), fontsize=10, fontname="hebo")
        
        # On cherche OBST et ST spécifiquement dans cette zone
        limiting_type = str(ai_data.get("obst_or_st_limiting", "")).upper()
        obst_r = p1.search_for("OBST", clip=r_lim)
        st_r = p1.search_for("ST", clip=r_lim)
        
        # On tire un trait rouge sur celui qui n'est PAS la limite
        if limiting_type == "ST" and obst_r:
            ox = obst_r[0]
            p1.draw_line((ox.x0, ox.y0 + (ox.y1-ox.y0)/2), (ox.x1, ox.y0 + (ox.y1-ox.y0)/2), color=red_color, width=1.5)
        elif limiting_type == "OBST" and st_r:
            sx = st_r[0]
            p1.draw_line((sx.x0, sx.y0 + (sx.y1-sx.y0)/2), (sx.x1, sx.y0 + (sx.y1-sx.y0)/2), color=red_color, width=1.5)

    # 3. DEST/ALTN Max Landing
    dest_altn_rects = p1.search_for("DEST/ALTN")
    if dest_altn_rects:
        r_dest = dest_altn_rects[0]
        p1.insert_text((r_dest.x1 + 5, r_dest.y1), str(ai_data.get("max_ldg", "")), fontsize=10, fontname="hebo")

    # 4. Escape Route
    if ai_data.get("escape_route"):
        rects = p1.search_for("1E0 ESCAPE PROCEDURE:")
        if rects:
            r = rects[0]
            text_rect = fitz.Rect(r.x0, r.y1 + 5, r.x0 + 350, r.y1 + 80)
            p1.insert_textbox(text_rect, ai_data["escape_route"], fontsize=8, fontname="helv", align=0)

    # 5. Type d'Ops (Sécurisé pour ne cibler que l'en-tête)
    ops_titles = p1.search_for("TYPE OF OPS")
    if ops_titles:
        r_title = ops_titles[0]
        # Zone de recherche restreinte juste à droite du titre
        search_rect = fitz.Rect(r_title.x1, r_title.y0 - 5, r_title.x1 + 150, r_title.y1 + 5)
        ops_rects = p1.search_for(user_params["ops"], clip=search_rect)
        if ops_rects:
            r = ops_rects[0]
            p1.draw_rect(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), color=red_color, width=1.5)

    # 6. Encadrement précis du PF / PM
    # On cherche "PF" et "PM" uniquement dans la moitié basse de la page (Crew)
    pf_rects = sorted([r for r in p1.search_for("PF") if r.y0 > 400], key=lambda x: x.x0)
    pm_rects = sorted([r for r in p1.search_for("PM") if r.y0 > 400], key=lambda x: x.x0)
    
    # On suppose que CPT est à gauche (index 0) et F/O à droite (index 1)
    if len(pf_rects) >= 2 and len(pm_rects) >= 2:
        if user_params["pf"] == "CPT":
            # Encadre PF pour CPT et PM pour FO
            p1.draw_rect(fitz.Rect(pf_rects[0].x0 - 2, pf_rects[0].y0 - 2, pf_rects[0].x1 + 2, pf_rects[0].y1 + 2), color=red_color, width=1.5)
            p1.draw_rect(fitz.Rect(pm_rects[1].x0 - 2, pm_rects[1].y0 - 2, pm_rects[1].x1 + 2, pm_rects[1].y1 + 2), color=red_color, width=1.5)
        else:
            # Encadre PM pour CPT et PF pour FO
            p1.draw_rect(fitz.Rect(pm_rects[0].x0 - 2, pm_rects[0].y0 - 2, pm_rects[0].x1 + 2, pm_rects[0].y1 + 2), color=red_color, width=1.5)
            p1.draw_rect(fitz.Rect(pf_rects[1].x0 - 2, pf_rects[1].y0 - 2, pf_rects[1].x1 + 2, pf_rects[1].y1 + 2), color=red_color, width=1.5)

    # --- PAGE 2 : ROUTE & MORA ---
    if len(doc) > 1:
        p2 = doc[1]
        
        if user_params.get("driftdown_fl"):
            draw_text_next_to(p2, "-TOC-", f"DD: FL {user_params['driftdown_fl']}", offset_x=15, color=red_color)

        words = p2.get_text("words")
        max_mora, max_mora_rect = 0, None
        for w in words:
            text = w[4]
            if text.isdigit() and 50 <= int(text) <= 250:
                if int(text) > max_mora:
                    max_mora = int(text)
                    max_mora_rect = fitz.Rect(w[:4])
                    
        if max_mora_rect:
            recovery_alt = (max_mora * 100) + 1000
            p2.insert_text((max_mora_rect.x1 + 20, max_mora_rect.y1), f"Rec: {recovery_alt} FT", fontsize=10, fontname="hebo", color=red_color)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

# =====================================================================
# SECTION 5 : INTERFACE UTILISATEUR (STREAMLIT)
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
        with st.spinner("Analyse approfondie (Météo/APG/WB) et tracé en cours..."):
            doc_wb = fitz.open(stream=wb_file.read(), filetype="pdf")
            wb_text = " ".join([page.get_text() for page in doc_wb])
            wb_text = " ".join(wb_text.split())
            doc_wb.close()
            
            fp_text = extract_text_from_fp(fp_file)

            ai_data = extract_data_with_ai(fp_text, wb_text, api_key)

            if ai_data:
                st.success("✅ Logique de Dispatch terminée avec succès.")
                
                # Le bloc expander vous permet de lire le raisonnement de l'IA (pratique pour vérifier ses déductions météo)
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
