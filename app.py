import streamlit as st
import fitz  # PyMuPDF : La bibliothèque la plus performante pour lire et éditer des PDF
import json
import io
import time
import google.generativeai as genai

# Configuration de base de la page web Streamlit
st.set_page_config(page_title="C525 Smart OFP Assistant", layout="wide", page_icon="✈️")

# =====================================================================
# SECTION 1 : EXTRACTION ET LECTURE DES DONNÉES (PDF -> TEXTE)
# =====================================================================

def extract_text_from_fp(fp_file):
    """
    Objectif : Extraire le texte du Flight Package sans saturer la mémoire du serveur.
    Problème résolu : Un FP contient souvent >100 pages de NOTAMs inutiles pour les perfos.
    Méthode : On lit uniquement les 15 premières pages (Météo/Route) et les 15 dernières (APG).
    
    Args:
        fp_file: Le fichier PDF uploadé via Streamlit.
    Returns:
        String: Le texte brut concaténé et nettoyé des pages sélectionnées.
    """
    doc_fp = fitz.open(stream=fp_file.read(), filetype="pdf")
    total_pages = len(doc_fp)
    
    # Création d'une liste des pages à lire (0 à 14)
    pages_to_read = list(range(min(15, total_pages)))
    
    # Si le document est très long, on ajoute les 15 dernières pages (pour choper l'APG)
    if total_pages > 30:
        pages_to_read += list(range(total_pages - 15, total_pages))
        
    # Extraction du texte en ignorant les doublons si le doc fait moins de 30 pages
    fp_text = " ".join([doc_fp[i].get_text() for i in set(pages_to_read)])
    doc_fp.close()
    
    # Nettoyage des espaces et retours à la ligne superflus pour aider l'IA
    fp_text = " ".join(fp_text.split())
    return fp_text

# =====================================================================
# SECTION 2 : ANALYSE INTELLIGENTE (TEXTE -> DONNÉES STRUCTURÉES)
# =====================================================================

def extract_data_with_ai(fp_text, wb_text, api_key):
    """
    Objectif : Utiliser l'IA Gemini pour comprendre le contexte du vol et extraire les valeurs.
    Problème résolu : Remplace les recherches par mots-clés (Regex) qui plantent si le format change.
    Méthode : Envoie un "Prompt" strict à Gemini 3.6 Flash exigeant une réponse en JSON pur, 
              avec une gestion avancée des erreurs de quota (limites gratuites).
    
    Args:
        fp_text (str): Le texte brut du Flight Package.
        wb_text (str): Le texte brut du Weight & Balance.
        api_key (str): La clé API Google secrète.
    Returns:
        Dict: Un dictionnaire Python (JSON) contenant toutes les valeurs, ou None si échec.
    """
    genai.configure(api_key=api_key)
    
    # Forçage du modèle spécifique exigé par l'API Google
    model = genai.GenerativeModel("gemini-3.6-flash")
    
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
    {fp_text[:8000]} # On limite à 8000 caractères pour ne pas surcharger la mémoire de l'IA
    
    TEXTE WEIGHT & BALANCE :
    {wb_text[:2000]}
    
    Réponds UNIQUEMENT avec ce format JSON strict (n'ajoute aucun commentaire ni balise markdown) :
    {{
        "tow": "", "zfw": "", "landing_weight": "", "to_power": "",
        "obst_limit": "", "lvl_off": "", "runway": "", "escape_route": "", "max_ldg": ""
    }}
    """
    
    # Boucle de tentative (Retry Logic) pour contourner les erreurs réseau ou de quota court
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            # Nettoyage de la réponse si l'IA inclut des balises Markdown (```json ... ```)
            json_str = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(json_str)
            
        except Exception as e:
            erreur = str(e)
            
            # Gestion des erreurs de Quota (Code 429)
            if "429" in erreur or "quota" in erreur.lower():
                # Si le quota journalier de 20 requêtes est épuisé (Free Tier)
                if "PerDay" in erreur:
                    st.error("❌ Quota journalier IA épuisé (20 requêtes/jour max). Veuillez utiliser une autre clé API ou réessayer demain.")
                    return None
                # Si c'est juste le quota par minute (5 requêtes/min), on patiente
                else:
                    if attempt < max_retries - 1:
                        st.warning(f"⏳ Quota IA atteint (Free Tier). Pause automatique de 60s pour réinitialisation...")
                        time.sleep(60)
                        continue
            
            # Si c'est une autre erreur (serveur Google HS, JSON mal formé, etc.)
            st.error(f"Erreur inattendue de l'IA : {erreur}")
            return None
            
    return None

# =====================================================================
# SECTION 3 : OUTILS DE DESSIN PAR ANCRAGE SPATIAL (GÉOMÉTRIE)
# =====================================================================

def draw_text_next_to(page, keyword, text, offset_x=5, offset_y=0, font="hebo", size=10, color=(0,0,0)):
    """
    Objectif : Trouver un mot-clé sur la page et écrire une donnée juste à sa droite.
    Exemple : Trouve "T/O POWER:" et écrit "98.6%" à +5 pixels sur la droite.
    
    Args:
        page: L'objet page PyMuPDF en cours d'édition.
        keyword (str): Le mot exact à chercher (ex: "RUNWAY:").
        text (str): La valeur à écrire.
        offset_x (int): Décalage horizontal par rapport au bord droit du mot-clé.
        offset_y (int): Décalage vertical (0 = aligné sur la même ligne de base).
    """
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0] # On prend la première apparition du mot sur la page
        # r.x1 est le bord droit du mot, r.y1 est la base du mot
        page.insert_text((r.x1 + offset_x, r.y1 + offset_y), str(text), fontsize=size, fontname=font, color=color)

def draw_text_above(page, keyword, text, offset_x=0, offset_y=-15, font="hebo", size=10, color=(0,0,0)):
    """
    Objectif : Trouver un mot-clé sur la page et écrire une donnée juste au-dessus.
    Exemple : Trouve "UPDATE:" et écrit la Landing Weight juste au-dessus.
    """
    if not text: return
    rects = page.search_for(keyword)
    if rects:
        r = rects[0]
        # r.x0 est le bord gauche du mot, r.y0 est le sommet du mot
        page.insert_text((r.x0 + offset_x, r.y0 + offset_y), str(text), fontsize=size, fontname=font, color=color)

# =====================================================================
# SECTION 4 : GÉNÉRATION DU PDF FINAL
# =====================================================================

def draw_on_pdf(template_bytes, ai_data, user_params):
    """
    Objectif : Moteur principal de dessin. Prend le PDF vierge, l'analyse avec les outils 
               de la Section 3, et applique les données de l'IA (Section 2) et les choix du pilote.
    
    Returns:
        io.BytesIO: Le fichier PDF généré en mémoire (prêt à être téléchargé).
    """
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    red_color = (1, 0, 0)
    
    # ------------------ PAGE 1 : EN-TÊTE ET PERFORMANCES ------------------
    p1 = doc[0]
    
    # 1. Écriture des données chiffrées
    draw_text_next_to(p1, "T/O POWER:", ai_data.get("to_power", ""))
    draw_text_next_to(p1, "RUNWAY:", ai_data.get("runway", ""))
    draw_text_next_to(p1, "TO WEIGHT:", ai_data.get("tow", ""))
    draw_text_next_to(p1, "OBST/ST LIMIT:", ai_data.get("obst_limit", ""))
    draw_text_next_to(p1, "LVL OFF:", ai_data.get("lvl_off", ""))
    draw_text_next_to(p1, "RMQ:", f"ZFW: {ai_data.get('zfw', '')}")
    draw_text_next_to(p1, "(DEST/ALTN):", ai_data.get("max_ldg", ""))
    
    # Le Landing Weight est placé spécifiquement au-dessus du mot UPDATE
    draw_text_above(p1, "UPDATE:", f"WB: Landing Weight {ai_data.get('landing_weight', '')}")

    # 2. Dessin de l'Escape Route dans un bloc de texte
    if ai_data.get("escape_route"):
        rects = p1.search_for("1E0 ESCAPE PROCEDURE:")
        if rects:
            r = rects[0]
            # Création d'une boîte virtuelle en dessous du titre pour contenir le long texte
            text_rect = fitz.Rect(r.x0, r.y1 + 5, r.x0 + 350, r.y1 + 80)
            p1.insert_textbox(text_rect, ai_data["escape_route"], fontsize=8, fontname="helv", align=0)

    # 3. Encadrer le Type d'Ops sélectionné par l'utilisateur (COM/PVT/TRG/MED)
    ops_rects = p1.search_for(user_params["ops"])
    for r in ops_rects:
        # On vérifie que le mot est bien dans le haut de la page (y0 < 300) pour ne pas 
        # entourer accidentellement le mot "COM" s'il apparaît ailleurs dans un NOTAM
        if r.y0 < 300: 
            # On dessine un rectangle légèrement plus grand que le mot avec des bords arrondis (radius)
            p1.draw_rect(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), color=red_color, width=1.5, radius=2)

    # 4. Encadrer dynamiquement le PF (Pilot Flying) et PM (Pilot Monitoring)
    pf_pm_rects = p1.search_for("PF-PM")
    # On filtre pour ne garder que ceux du bloc "Temps de vol" et on les trie de gauche à droite
    pf_pm_rects = sorted([r for r in pf_pm_rects if 600 < r.y0 < 750], key=lambda x: x.x0)
    
    if len(pf_pm_rects) >= 2:
        cpt_rect = pf_pm_rects[0] # Le premier trouvé à gauche
        fo_rect = pf_pm_rects[1]  # Le deuxième trouvé à droite

        def draw_circle(r, role):
            """Fonction interne pour tracer une boîte soit autour du 'PF', soit autour du 'PM'."""
            w = r.x1 - r.x0
            if role == "PF": 
                # Boîte sur la moitié gauche du texte "PF-PM"
                box = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x0 + w/2, r.y1 + 2)
            else: 
                # Boîte sur la moitié droite du texte "PF-PM"
                box = fitz.Rect(r.x0 + w/2, r.y0 - 2, r.x1 + 2, r.y1 + 2)
            p1.draw_rect(box, color=red_color, width=1.5, radius=3)

        # Application de la sélection utilisateur
        if user_params["pf"] == "CPT":
            draw_circle(cpt_rect, "PF")
            draw_circle(fo_rect, "PM")
        else:
            draw_circle(cpt_rect, "PM")
            draw_circle(fo_rect, "PF")

    # ------------------ PAGE 2 : ROUTE & MORA ------------------
    if len(doc) > 1:
        p2 = doc[1]
        
        # 5. Drift Down : On l'écrit à droite du point "-TOC-"
        if user_params.get("driftdown_fl"):
            draw_text_next_to(p2, "-TOC-", f"DD: FL {user_params['driftdown_fl']}", offset_x=15, color=red_color)

        # 6. Recherche de la MORA Maximum pour l'altitude de Recovery
        words = p2.get_text("words")
        max_mora, max_mora_rect = 0, None
        for w in words:
            text = w[4]
            # La MORA est généralement un nombre entre 50 et 250 (soit 5000ft à 25000ft)
            if text.isdigit() and 50 <= int(text) <= 250:
                if int(text) > max_mora:
                    max_mora = int(text)
                    max_mora_rect = fitz.Rect(w[:4]) # On sauvegarde la position de la plus haute
                    
        # Écriture de la Recovery Altitude à côté de la MORA la plus haute
        if max_mora_rect:
            recovery_alt = (max_mora * 100) + 1000
            p2.insert_text((max_mora_rect.x1 + 20, max_mora_rect.y1), f"Rec: {recovery_alt} FT", fontsize=10, fontname="hebo", color=red_color)

    # ------------------ SAUVEGARDE EN MÉMOIRE ------------------
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
    fp_file = st.file_uploader("1. Flight Package (PDF)", type="pdf", help="Le FP complet avec Météo et APG.")
with col2: 
    wb_file = st.file_uploader("2. Weight & Balance (PDF)", type="pdf", help="La Loadsheet finale.")
with col3: 
    template_file = st.file_uploader("3. OFP PPS (PDF)", type="pdf", help="L'OFP pré-rempli par le système à compléter.")

st.markdown("### ⚙️ Paramètres du vol")
col_pf, col_ops, col_dd = st.columns(3)
with col_pf: 
    pf = st.radio("Pilot Flying (PF) :", ["CPT", "FO"], horizontal=True)
with col_ops: 
    ops = st.radio("Type of Ops :", ["COM", "PVT", "TRG", "MED"], horizontal=True)
with col_dd: 
    driftdown_fl = st.text_input("Drift Down FL :", placeholder="ex: 220")

# Bouton d'action principal
if st.button("🚀 Analyser avec l'IA & Compléter l'OFP", type="primary", use_container_width=True):
    
    # Vérification vitale 1 : Clé API
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
    except Exception:
        st.error("⚠️ La clé API n'est pas configurée dans les Secrets de Streamlit.")
        st.stop()

    # Vérification vitale 2 : Présence des fichiers
    if not (fp_file and wb_file and template_file):
        st.warning("⚠️ Veuillez charger les 3 documents PDF avant de lancer l'analyse.")
    else:
        with st.spinner("Extraction, analyse sémantique (IA) et tracé géométrique en cours..."):
            
            # --- Étape A : Lecture des fichiers ---
            doc_wb = fitz.open(stream=wb_file.read(), filetype="pdf")
            wb_text = " ".join([page.get_text() for page in doc_wb])
            wb_text = " ".join(wb_text.split()) # Nettoyage
            doc_wb.close()
            
            fp_text = extract_text_from_fp(fp_file)

            # --- Étape B : Intelligence Artificielle ---
            ai_data = extract_data_with_ai(fp_text, wb_text, api_key)

            if ai_data:
                st.success("✅ Données extraites de l'APG et du W&B avec succès !")
                
                # Optionnel : Afficher ce que l'IA a compris pour vérification par l'équipage
                with st.expander("🔍 Vérifier les données brutes extraites"):
                    st.json(ai_data)
                
                # --- Étape C : Tracé sur le PDF ---
                user_params = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
                template_bytes = template_file.read()
                
                final_pdf = draw_on_pdf(template_bytes, ai_data, user_params)

                # --- Étape D : Bouton de téléchargement final ---
                st.download_button(
                    label="📥 Télécharger l'OFP Complété pour le vol",
                    data=final_pdf,
                    file_name="OFP_Smart_Complete.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )
