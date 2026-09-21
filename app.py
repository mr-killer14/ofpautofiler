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
    """
    Extrait le texte utile du Flight Package sans saturer la mémoire.
    Lit les 15 premières pages (Météo/Route) et les 15 dernières (APG),
    en ignorant le bloc central de NOTAMs.
    """
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
# SECTION 2 : ANALYSE INTELLIGENTE VIA GROQ (MODÈLE DYNAMIQUE)
# =====================================================================

def extract_data_with_ai(fp_text, wb_text, api_key):
    """
    Détecte automatiquement les modèles actifs sur le compte Groq
    et extrait les données de vol en JSON pur.
    """
    client = Groq(api_key=api_key)
    
    try:
        models_data = client.models.list().data
        active_models = [
            m.id for m in models_data 
            if "whisper" not in m.id.lower() and "guard" not in m.id.lower()
        ]
        
        if not active_models:
            st.error("❌ Aucun modèle de génération textuelle disponible sur ce compte Groq.")
            return None

        selected_model = active_models[0]
        for m_id in active_models:
            if "llama-3" in m_id.lower():
                selected_model = m_id
                break
            elif "mixtral" in m_id.lower():
                selected_model = m_id
                
        st.info(f"🤖 Modèle sélectionné sur votre compte Groq : `{selected_model}`")

        prompt = f"""
        Tu es un dispatcher aéronautique expert. Analyse ces documents de vol brut (Flight Package contenant Météo et Perfos APG, et une Loadsheet).
        Extrais les informations exactes demandées en format JSON pur.
        
        Règles strictes d'extraction :
        - tow : masse au décollage RÉELLE (pas la structurelle Max).
        - zfw : zero fuel weight RÉEL (pas la structurelle Max).
        - landing_weight : landing weight RÉEL (pas la structurelle Max).
        - to_power : la puissance de décollage (ex: 98.6%) correspondant à la température OAT du METAR de départ et à la bonne piste.
        - obst_limit : la masse limite d'obstacle APG pour le décollage.
        - lvl_off : l'altitude de Level Off APG en cas de panne moteur.
        - runway : la piste de décollage utilisée dans l'APG.
        - escape_route : le texte de la SPECIAL DEPARTURE PROCEDURE (NOTE: NON-RNAV PROCEDURE...).
        - max_ldg : sous la forme "DEST/ALTN" (ex: "9900/9900"), en utilisant les perfos APG "LANDING PERFORMANCE" adaptées aux METAR (Dry ou Wet).
        
        TEXTE FLIGHT PACKAGE & APG :
        {fp_text[:8000]} 
        
        TEXTE WEIGHT & BALANCE :
        {wb_text[:2000]}
        
        Réponds UNIQUEMENT avec ce format JSON strict (n'ajoute aucun commentaire ni balise markdown) :
        {{
            "tow": "", "zfw": "", "landing_weight": "", "to_power": "",
            "obst_limit": "", "lvl_off": "", "runway": "", "escape_route": "", "max_ldg": ""
        }}
        """
        
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=selected_model,
            temperature=0,
        )
        
        response_text = chat_completion.choices[0].message.content
        json_str = response_text.replace("```json", "").replace("```", "").strip()
        return json.loads(json_str)
        
    except Exception as e:
        st.error(f"Erreur lors de l'analyse IA (Groq) : {str(e)}")
        return None

# =====================================================================
# SECTION 3 : OUTILS DE DESSIN PAR ANCRAGE SPATIAL (GÉOMÉTRIE)
# =====================================================================

def draw_text_next_to(page, keyword, text, offset_x=5, offset_y=0, font="hebo", size=10, color=(0,0,0)):
    """Trouve un mot-clé sur la page et écrit la donnée juste à sa droite."""
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0]
        page.insert_text((r.x1 + offset_x, r.y1 + offset_y), str(text), fontsize=size, fontname=font, color=color)

def draw_text_above(page, keyword, text, offset_x=0, offset_y=-15, font="hebo", size=10, color=(0,0,0)):
    """Trouve un mot-clé sur la page et écrit la donnée juste au-dessus."""
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0]
        page.insert_text((r.x0 + offset_x, r.y0 + offset_y), str(text), fontsize=size, fontname=font, color=color)

# =====================================================================
# SECTION 4 : GÉNÉRATION DU PDF FINAL
# =====================================================================

def draw_on_pdf(template_bytes, ai_data, user_params):
    """Applique les données extraites et les choix d'équipage sur l'OFP PPS vierge."""
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    red_color = (1, 0, 0)
    
    # --- PAGE 1 : EN-TÊTE ET PERFORMANCES ---
    p1 = doc[0]
    
    draw_text_next_to(p1, "T/O POWER:", ai_data.get("to_power", ""))
    draw_text_next_to(p1, "RUNWAY:", ai_data.get("runway", ""))
    draw_text_next_to(p1, "TO WEIGHT:", ai_data.get("tow", ""))
    draw_text_next_to(p1, "OBST/ST LIMIT:", ai_data.get("obst_limit", ""))
    draw_text_next_to(p1, "LVL OFF:", ai_data.get("lvl_off", ""))
    
    if ai_data.get("zfw"):
        draw_text_next_to(p1, "RMQ:", f"ZFW: {ai_data.get('zfw')}")
        
    draw_text_next_to(p1, "(DEST/ALTN):", ai_data.get("max_ldg", ""))
    
    if ai_data.get("landing_weight"):
        draw_text_above(p1, "UPDATE:", f"WB: Landing Weight {ai_data.get('landing_weight')}")

    if ai_data.get("escape_route"):
        rects = p1.search_for("1E0 ESCAPE PROCEDURE:")
        if rects:
            r = rects[0]
            text_rect = fitz.Rect(r.x0, r.y1 + 5, r.x0 + 350, r.y1 + 80)
            p1.insert_textbox(text_rect, ai_data["escape_route"], fontsize=8, fontname="helv", align=0)

    # Encadrement du type d'opération (sans radius)
    ops_rects = p1.search_for(user_params["ops"])
    for r in ops_rects:
        if r.y0 < 300:
            p1.draw_rect(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), color=red_color, width=1.5)

    # Encadrement du PF et PM (sans radius)
    pf_pm_rects = p1.search_for("PF-PM")
    pf_pm_rects = sorted([r for r in pf_pm_rects if 600 < r.y0 < 750], key=lambda x: x.x0)
    
    if len(pf_pm_rects) >= 2:
        cpt_rect = pf_pm_rects[0]
        fo_rect = pf_pm_rects[1]

        def draw_box(r, role):
            w = r.x1 - r.x0
            if role == "PF": 
                box = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x0 + w/2, r.y1 + 2)
            else: 
                box = fitz.Rect(r.x0 + w/2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
            p1.draw_rect(box, color=red_color, width=1.5)

        if user_params["pf"] == "CPT":
            draw_box(cpt_rect, "PF")
            draw_box(fo_rect, "PM")
        else:
            draw_box(cpt_rect, "PM")
            draw_box(fo_rect, "PF")

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
        with st.spinner("Analyse et tracé en cours..."):
            doc_wb = fitz.open(stream=wb_file.read(), filetype="pdf")
            wb_text = " ".join([page.get_text() for page in doc_wb])
            wb_text = " ".join(wb_text.split())
            doc_wb.close()
            
            fp_text = extract_text_from_fp(fp_file)

            ai_data = extract_data_with_ai(fp_text, wb_text, api_key)

            if ai_data:
                st.success("✅ Analyse terminée avec succès.")
                with st.expander("🔍 Vérifier les données brutes extraites"):
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
