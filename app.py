import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import re
import io
import textwrap

st.set_page_config(page_title="C525 Smart OFP", layout="wide")

# --- FONCTIONS D'EXTRACTION ---

def get_best_runway(wind_dir_str, runways_str):
    if not wind_dir_str or wind_dir_str == "VRB" or not runways_str: 
        return runways_str.split()[0] if runways_str else ""
    wind_dir = int(wind_dir_str)
    runways = re.findall(r'\d{2}[A-Z]?', runways_str)
    best_rwy, min_diff = "", 180
    for rwy in runways:
        rwy_hdg = int(rwy[:2]) * 10
        diff = abs(wind_dir - rwy_hdg)
        if diff > 180: diff = 360 - diff
        if diff < min_diff:
            min_diff, best_rwy = diff, rwy
    return best_rwy

def analyze_weather(text, airport):
    """Extrait OAT, vent et déduit WET/DRY depuis le METAR."""
    oat, wind_dir, condition, runways = None, None, "DRY", ""
    rwy_match = re.search(rf'{airport}.*?RWY\s+([\w\s]+)', text)
    if rwy_match: runways = rwy_match.group(1).strip()
    
    metar_match = re.search(rf'METAR\s+.*?{airport}.*?(\d{{3}}|VRB)(\d{{2,3}})G?\d*KT.*?\s+(M?\d{{2}})/(M?\d{{2}})', text, re.DOTALL)
    if metar_match:
        wind_dir = metar_match.group(1)
        oat = metar_match.group(3).replace('M', '-')
        condition = "WET" if re.search(r'(RA|DZ|SN|SHRA|FG|BR|HZ)', metar_match.group(0)) else "DRY"
    return oat, get_best_runway(wind_dir, runways), condition

def extract_apg_wb_data(fp_text, wb_text, flight_data):
    """Extrait intelligemment l'APG et le W&B."""
    # Weight & Balance
    tow = re.search(r'TOW\s+(?:Max\s*\d+\s+)?(?:OK\s+)?(\d{4,5})', wb_text)
    zfw = re.search(r'ZFW\s+(?:Max\s*\d+\s+)?(?:OK\s+)?(\d{4,5})', wb_text)
    lw = re.search(r'LW\s+(?:Max\s*\d+\s+)?(?:OK\s+)?(\d{4,5})', wb_text)
    if tow: flight_data['tow'] = tow.group(1)
    if zfw: flight_data['zfw'] = zfw.group(1)
    if lw: flight_data['landing_weight'] = lw.group(1)

    # Aéroports pour l'APG
    route = re.search(r'DEP\s+([A-Z]{4}).*?DEST\s+([A-Z]{4}).*?ALTN\s+([A-Z]{4})', fp_text)
    if route:
        dep, dest, alt = route.group(1), route.group(2), route.group(3)
    else:
        return

    # Météo et APG
    dep_oat, dep_rwy, dep_cond = analyze_weather(fp_text, dep)
    _, dest_rwy, dest_cond = analyze_weather(fp_text, dest)
    _, alt_rwy, alt_cond = analyze_weather(fp_text, alt)

    # Takeoff Perfos (Recherche dynamique selon Piste et Condition)
    to_match = re.search(rf'TAKEOFF PERFORMANCE.*?{dep}.*?Runway\s+{dep_rwy}.*?(\d{{2}}\.\d)\s+(\d{{4,5}})\s+[A-Z]+\s+[\d/]+\s+(\d{{3,4}})', fp_text, re.DOTALL | re.IGNORECASE)
    if not to_match: # Fallback global si piste non trouvée
        to_match = re.search(rf'TAKEOFF PERFORMANCE.*?{dep}.*?(\d{{2}}\.\d)\s+(\d{{4,5}})\s+[A-Z]+\s+[\d/]+\s+(\d{{3,4}})', fp_text, re.DOTALL)
    
    if to_match:
        flight_data['to_power'] = to_match.group(1) + "%"
        flight_data['obst_limit'] = to_match.group(2)
        flight_data['lvl_off'] = to_match.group(3)
        flight_data['runway'] = dep_rwy

    # Escape Route
    escape_match = re.search(r'SPECIAL DEPARTURE PROCEDURES.*?NOTE: NON-RNAV PROCEDURE.*?(?=###)', fp_text, re.DOTALL)
    if escape_match:
        cl = escape_match.group(0).replace('b ', '').replace('\n', ' ')
        flight_data['escape_route'] = re.sub(r'\s+', ' ', cl).strip()

    # Landing Perfos
    max_dest, max_alt = "9900", "9900"
    dest_match = re.search(rf'LANDING PERFORMANCE.*?{dest}.*?COND:\s*{dest_cond}.*?2\.5%\s+(\d{{4}})', fp_text, re.DOTALL | re.IGNORECASE)
    if dest_match: max_dest = dest_match.group(1)
    
    alt_match = re.search(rf'LANDING PERFORMANCE.*?{alt}.*?COND:\s*{alt_cond}.*?2\.5%\s+(\d{{4}})', fp_text, re.DOTALL | re.IGNORECASE)
    if alt_match: max_alt = alt_match.group(1)
        
    flight_data['max_ldg'] = f"{max_dest}/{max_alt}"

# --- FONCTION DE DESSIN INTELLIGENT (CTRL+F) ---

def draw_on_pdf(template_bytes, flight_data):
    """Utilise PyMuPDF pour chercher les mots sur la page et écrire à côté."""
    doc = fitz.open(stream=template_bytes, filetype="pdf")
    red_color = (1, 0, 0) # Rouge pour le dessin
    black_color = (0, 0, 0)

    # PAGE 1 : Remplissage des champs
    p1 = doc[0]
    
    # Dictionnaire de ce qu'on cherche : ce qu'on écrit
    to_write = {
        "T/O POWER:": flight_data.get("to_power", ""),
        "RUNWAY:": flight_data.get("runway", ""),
        "TO WEIGHT:": flight_data.get("tow", ""),
        "OBST/ST LIMIT:": flight_data.get("obst_limit", ""),
        "LVL OFF:": flight_data.get("lvl_off", ""),
        "RMQ:": f"ZFW: {flight_data.get('zfw', '')}",
        "UPDATE:": f"WB: Landing Weight {flight_data.get('landing_weight', '')}",
        "(DEST/ALTN):": flight_data.get("max_ldg", "")
    }

    # Écriture par recherche (Ancrage)
    for search_text, value_to_write in to_write.items():
        if not value_to_write: continue
        rects = p1.search_for(search_text)
        if rects:
            rect = rects[0] # Prend la première occurrence
            # On insère le texte juste à droite de la boîte du mot trouvé
            p1.insert_text((rect.x1 + 5, rect.y1), value_to_write, fontsize=10, fontname="helv", color=black_color)

    # Escape Route (Dessin dans un rectangle spécifique)
    if flight_data.get("escape_route"):
        rects = p1.search_for("1E0 ESCAPE PROCEDURE:")
        if rects:
            rect = rects[0]
            # On définit une zone de texte à droite de ce titre
            text_rect = fitz.Rect(rect.x1 + 10, rect.y0 - 10, rect.x1 + 250, rect.y0 + 60)
            p1.insert_textbox(text_rect, flight_data["escape_route"], fontsize=8, fontname="helv", align=0)

    # Entourer les sélections (PF/PM et OPS)
    ops_rects = p1.search_for(flight_data.get("ops", ""))
    for r in ops_rects:
        if 700 < r.y0 < 800: 
            p1.draw_rect(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), color=red_color, width=1.5)

    cpt_rects = p1.search_for("CPT")
    fo_rects = p1.search_for("F/O")
    
    if cpt_rects and fo_rects:
        cpt_rect = [r for r in cpt_rects if 650 < r.y0 < 720]
        fo_rect = [r for r in fo_rects if 650 < r.y0 < 720]
        
        if cpt_rect and fo_rect:
            cpt_r = cpt_rect[0]
            fo_r = fo_rect[0]
            pf_y = cpt_r.y1 + 10 
            
            if flight_data["pf"] == "CPT":
                p1.draw_rect(fitz.Rect(cpt_r.x0 - 5, pf_y, cpt_r.x1 + 5, pf_y + 12), color=red_color, width=1.5)
                p1.draw_rect(fitz.Rect(fo_r.x0 + 20, pf_y, fo_r.x1 + 15, pf_y + 12), color=red_color, width=1.5)
            else:
                p1.draw_rect(fitz.Rect(cpt_r.x0 + 20, pf_y, cpt_r.x1 + 20, pf_y + 12), color=red_color, width=1.5)
                p1.draw_rect(fitz.Rect(fo_r.x0 - 5, pf_y, fo_r.x1 + 5, pf_y + 12), color=red_color, width=1.5)

    # PAGE 2 : Drift Down et MORA
    if len(doc) > 1:
        p2 = doc[1]
        
        if flight_data.get("driftdown_fl"):
            toc_rects = p2.search_for("-TOC-")
            if toc_rects:
                r = toc_rects[0]
                p2.insert_text((r.x1 + 20, r.y1), f"DD: FL {flight_data['driftdown_fl']}", fontsize=10, fontname="hebo", color=red_color)

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

# --- INTERFACE WEB STREAMLIT ---

st.markdown("### 🛫 Documents de vol")
col1, col2, col3 = st.columns(3)
with col1: fp_file = st.file_uploader("1. Flight Package (PDF)", type="pdf")
with col2: wb_file = st.file_uploader("2. Weight & Balance (PDF)", type="pdf")
with col3: template_file = st.file_uploader("3. OFP vierge (PDF)", type="pdf")

st.markdown("### ⚙️ Paramètres du vol")
col_pf, col_ops, col_dd = st.columns(3)
with col_pf: pf = st.radio("Pilot Flying (PF) :", ["CPT", "FO"], horizontal=True)
with col_ops: ops = st.radio("Type of Ops :", ["COM", "PVT", "TRG", "MED"], horizontal=True)
with col_dd: driftdown_fl = st.text_input("Drift Down FL :", placeholder="ex: 220")

if st.button("🚀 Compléter l'OFP", type="primary", use_container_width=True):
    if not (fp_file and wb_file and template_file):
        st.warning("⚠️ Veuillez charger les 3 PDF.")
    else:
        with st.spinner("Analyse visuelle avec PyMuPDF en cours..."):
            flight_data = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
            
            # W&B
            with pdfplumber.open(wb_file) as pdf:
                wb_text = re.sub(r'\s+', ' ', re.sub(r'\|', ' ', pdf.pages[0].extract_text()))

            # Flight Package
            with pdfplumber.open(fp_file) as pdf:
                fp_text = " ".join([p.extract_text() for p in pdf.pages if p.extract_text()])
                fp_text = re.sub(r'\s+', ' ', re.sub(r'\|', ' ', fp_text))

            # Extraction
            extract_apg_wb_data(fp_text, wb_text, flight_data)

            # Dessin et Sauvegarde
            template_bytes = template_file.read()
            final_pdf = draw_on_pdf(template_bytes, flight_data)

            st.success("✅ L'OFP a été complété intelligemment !")
            st.download_button(
                label="📥 Télécharger l'OFP complété",
                data=final_pdf,
                file_name="OFP_Complete.pdf",
                mime="application/pdf",
                use_container_width=True
            )
