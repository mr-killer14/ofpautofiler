import streamlit as st
import pdfplumber
import re
import io
import textwrap
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import black, red

# ==========================================
# 🎯 COORDONNÉES EXACTES (X, Y)
# Basées sur les documents OFP vierge.pdf et OFP remplie.pdf
# ==========================================
POSITIONS_P1 = {
    # En-tête (Gauche)
    "flight_number": (60, 814),
    "departure":     (60, 785),
    "destination":   (60, 770),
    "alternate":     (60, 755),
    
    # En-tête (Droite)
    "tow":           (430, 740),
    
    # Bloc Fuel (Tableau de droite)
    "trip_fuel":     (535, 642),
    "cont_fuel":     (535, 627),
    "alt_fuel":      (535, 612),
    "final_fuel":    (535, 582),
    "ramp_fuel":     (535, 537),
    "landing_fuel":  (535, 477),
    
    # RMQ / Autres
    "zfw":           (90, 442),
    "to_power":      (100, 420),
    "obst_limit":    (100, 405),
    "lvl_off":       (100, 390),
    "max_ldg":       (300, 260),
    "landing_weight":(130, 245),
}

# Coordonnées des éléments à entourer (X, Y, Largeur, Hauteur)
BOXES = {
    # PF / PM (ligne CPT / F/O)
    "PF_CPT": (95, 680, 25, 12),
    "PM_CPT": (130, 680, 25, 12),
    "PF_FO":  (155, 680, 25, 12),
    "PM_FO":  (185, 680, 25, 12),
    
    # Type Ops
    "OPS_COM": (355, 765, 30, 12),
    "OPS_PVT": (388, 765, 27, 12),
    "OPS_TRG": (417, 765, 27, 12),
    "OPS_MED": (446, 765, 30, 12),
}

st.set_page_config(page_title="C525 Smart OFP", layout="wide")

# --- FONCTIONS D'ANALYSE ---

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'\|', ' ', text))

def get_best_runway(wind_dir_str, runways_str):
    if not wind_dir_str or wind_dir_str == "VRB" or not runways_str: 
        return runways_str.split()[0] if runways_str else ""
    wind_dir = int(wind_dir_str)
    runways = re.findall(r'\d{2}[A-Z]?', runways_str)
    best_rwy = ""
    min_diff = 180
    for rwy in runways:
        rwy_hdg = int(rwy[:2]) * 10
        diff = abs(wind_dir - rwy_hdg)
        if diff > 180: diff = 360 - diff
        if diff < min_diff:
            min_diff = diff
            best_rwy = rwy
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
        # Conditions WET (Pluie, Neige, etc.)
        condition = "WET" if re.search(r'(RA|DZ|SN|SHRA|FG|BR|HZ)', metar_match.group(0)) else "DRY"
        
    best_rwy = get_best_runway(wind_dir, runways)
    return oat, best_rwy, condition

def extract_apg_data(full_text, flight_data):
    """Cherche les perfos T/O et LDG dans le texte APG."""
    dep = flight_data.get('departure', '')
    dest = flight_data.get('destination', '')
    alt = flight_data.get('alternate', '')
    
    if dep:
        to_match = re.search(rf'TAKEOFF PERFORMANCE.*?{dep}.*?(\d{{2}}\.\d)\s+(\d{{4,5}})\s+[A-Z]+\s+[\d/]+\s+(\d{{3,4}})', full_text, re.DOTALL)
        if to_match:
            flight_data['to_power'] = to_match.group(1) + "%"
            flight_data['obst_limit'] = to_match.group(2)
            flight_data['lvl_off'] = to_match.group(3)

    escape_match = re.search(r'SPECIAL DEPARTURE PROCEDURES.*?NOTE: NON-RNAV PROCEDURE.*?(?=###)', full_text, re.DOTALL)
    if escape_match:
        cl = escape_match.group(0).replace('b ', '').replace('\n', ' ')
        flight_data['escape_route'] = re.sub(r'\s+', ' ', cl).strip()

    max_dest, max_alt = "9900", "9900"
    if dest:
        dest_cond = flight_data.get('dest_cond', 'DRY').upper()
        # Cherche la ligne 2.5% dans la section atterrissage
        dest_match = re.search(rf'LANDING PERFORMANCE.*?{dest}.*?COND:\s*{dest_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if dest_match: max_dest = dest_match.group(1)
    if alt:
        alt_cond = flight_data.get('alt_cond', 'DRY').upper()
        alt_match = re.search(rf'LANDING PERFORMANCE.*?{alt}.*?COND:\s*{alt_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if alt_match: max_alt = alt_match.group(1)
        
    flight_data['max_ldg'] = f"{max_dest}/{max_alt}"

def get_dynamic_coordinates(pdf_file):
    """Scanne la page 2 pour trouver les hauteurs du TOC et de la Grid MORA."""
    toc_coords, mora_coords, max_mora = None, None, 0
    try:
        with pdfplumber.open(pdf_file) as pdf:
            if len(pdf.pages) > 1:
                page2 = pdf.pages[1]
                for w in page2.extract_words():
                    if "-TOC-" in w['text']:
                        # Coordonnées figées en X (colonne RMKS), hauteur dynamique Y
                        toc_coords = (175, 842 - w['top'])
                    if w['text'].isdigit() and 50 <= int(w['text']) <= 250:
                        if int(w['text']) > max_mora:
                            max_mora = int(w['text'])
                            # Coordonnées figées en X (colonne RMKS), hauteur dynamique Y
                            mora_coords = (425, 842 - w['top'])
    except: pass
    return toc_coords, mora_coords, max_mora

def draw_pdf(template_bytes, flight_data, toc_coords, mora_coords, max_mora):
    """Dessine les textes et les cadres rouges sur le PDF."""
    packet1 = io.BytesIO()
    can1 = canvas.Canvas(packet1, pagesize=A4)
    can1.setFont("Helvetica-Bold", 10)
    can1.setFillColor(black)
    
    # 1. Écriture des valeurs page 1
    for key, text_val in flight_data.items():
        if key in POSITIONS_P1 and text_val:
            x, y = POSITIONS_P1[key]
            can1.drawString(x, y, str(text_val))
            
    # 2. Escape Route (dans la zone vide à côté de 1E0 ESCAPE PROCEDURE)
    if flight_data.get('escape_route'):
        textobject = can1.beginText(230, 435)
        textobject.setFont("Helvetica", 8)
        lines = textwrap.wrap(flight_data['escape_route'], width=65)
        for line in lines: textobject.textLine(line)
        can1.drawText(textobject)

    # 3. Encadrés PF/PM et OPS (Rouge)
    can1.setStrokeColor(red)
    can1.setLineWidth(1.5)
    can1.setFillColor(red)
    can1.setFillAlpha(0.0) # Fond transparent
    
    # Cercles PF/PM
    if flight_data['pf'] == "CPT":
        box_pf = BOXES["PF_CPT"]
        box_pm = BOXES["PM_FO"]
    else:
        box_pm = BOXES["PM_CPT"]
        box_pf = BOXES["PF_FO"]
        
    can1.roundRect(box_pf[0], box_pf[1], box_pf[2], box_pf[3], 4)
    can1.roundRect(box_pm[0], box_pm[1], box_pm[2], box_pm[3], 4)

    # Cercles Type OPS
    ops_box = BOXES.get(f"OPS_{flight_data['ops']}")
    if ops_box:
        can1.roundRect(ops_box[0], ops_box[1], ops_box[2], ops_box[3], 4)

    can1.save()
    packet1.seek(0)
    
    # --- PAGE 2 ---
    packet2 = io.BytesIO()
    can2 = canvas.Canvas(packet2, pagesize=A4)
    can2.setFont("Helvetica-Bold", 9)
    can2.setFillColor(red) # Écritures rouges pour bien les voir
    
    # Drift down
    if toc_coords and flight_data.get('driftdown_fl'):
        can2.drawString(toc_coords[0], toc_coords[1], f"DD: FL {flight_data['driftdown_fl']}")
        
    # Recovery Altitude
    if mora_coords:
        recovery_alt = (max_mora * 100) + 1000
        can2.drawString(mora_coords[0], mora_coords[1], f"Rec: {recovery_alt} FT")
        
    can2.save()
    packet2.seek(0)

    # --- FUSION ---
    template_reader = PdfReader(template_bytes)
    writer = PdfWriter()
    overlay1 = PdfReader(packet1).pages[0]
    overlay2 = PdfReader(packet2).pages[0]

    for i, page in enumerate(template_reader.pages):
        if i == 0: page.merge_page(overlay1)
        elif i == 1: page.merge_page(overlay2)
        writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output

# --- INTERFACE WEB STREAMLIT ---

st.markdown("### 🛫 Documents de vol")
col1, col2, col3 = st.columns(3)
with col1: fp_file = st.file_uploader("1. Flight Package (PDF)", type="pdf")
with col2: wb_file = st.file_uploader("2. Weight & Balance (PDF)", type="pdf")
with col3: template_file = st.file_uploader("3. Modèle OFP Vierge (PDF)", type="pdf")

st.markdown("### ⚙️ Paramètres du vol")
col_pf, col_ops, col_dd = st.columns(3)
with col_pf: pf = st.radio("Pilot Flying (PF) :", ["CPT", "FO"], horizontal=True)
with col_ops: ops = st.radio("Type of Ops :", ["COM", "PVT", "TRG", "MED"], horizontal=True)
with col_dd: driftdown_fl = st.text_input("Drift Down FL :", placeholder="ex: 220")

if st.button("🚀 Analyser & Générer l'OFP", type="primary", use_container_width=True):
    if not (fp_file and wb_file and template_file):
        st.warning("⚠️ Veuillez charger le Flight Package, le Weight & Balance et le Modèle OFP.")
    else:
        with st.spinner("Analyse APG, Météo et W&B en cours..."):
            flight_data = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
            
            # Parsing W&B
            with pdfplumber.open(wb_file) as pdf:
                text = clean_text(pdf.pages[0].extract_text())
                tow = re.search(r'TOW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                zfw = re.search(r'ZFW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                lw = re.search(r'LW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                
                if tow: flight_data['tow'] = tow.group(1)
                if zfw: flight_data['zfw'] = zfw.group(1)
                if lw: flight_data['landing_weight'] = lw.group(1)

            # Parsing Flight Package
            with pdfplumber.open(fp_file) as pdf:
                full_text = " ".join([p.extract_text() for p in pdf.pages if p.extract_text()])
                clean_full = clean_text(full_text)
                
                # Vol, Route, Fuel
                route = re.search(r'DEP\s+([A-Z]{4}).*?DEST\s+([A-Z]{4}).*?ALTN\s+([A-Z]{4})', clean_full)
                if route:
                    flight_data['departure'] = route.group(1)
                    flight_data['destination'] = route.group(2)
                    flight_data['alternate'] = route.group(3)
                    
                flt = re.search(r'FLT\s+([A-Z0-9]+)', clean_full)
                if flt: flight_data['flight_number'] = flt.group(1)
                
                trip = re.search(r'TRIP\s+(?:\d:\d{2}\s+)?(\d{3,5})', clean_full)
                if trip: flight_data['trip_fuel'] = trip.group(1)
                cont = re.search(r'CONT 5%\s+(\d{1,4})', clean_full)
                if cont: flight_data['cont_fuel'] = cont.group(1)
                alt1 = re.search(r'ALT1.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if alt1: flight_data['alt_fuel'] = alt1.group(1)
                fin = re.search(r'FINAL RSRV.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if fin: flight_data['final_fuel'] = fin.group(1)
                ramp = re.search(r'RAMP MREQ\s+(\d{3,5})', clean_full)
                if ramp: flight_data['ramp_fuel'] = ramp.group(1)
                
                # Calcul atterrissage : Ramp - Trip
                if flight_data.get('ramp_fuel') and flight_data.get('trip_fuel'):
                    flight_data['landing_fuel'] = str(int(flight_data['ramp_fuel']) - int(flight_data['trip_fuel']))

                # Météo + APG
                dep_oat, flight_data['dep_rwy'], dep_cond = analyze_weather(full_text, flight_data.get('departure'))
                _, flight_data['dest_rwy'], flight_data['dest_cond'] = analyze_weather(full_text, flight_data.get('destination'))
                _, flight_data['alt_rwy'], flight_data['alt_cond'] = analyze_weather(full_text, flight_data.get('alternate'))

                extract_apg_data(full_text, flight_data)

            # Dessin
            template_file.seek(0)
            toc_coords, mora_coords, max_mora = get_dynamic_coordinates(template_file)
            final_pdf = draw_pdf(template_file, flight_data, toc_coords, mora_coords, max_mora)

            st.success("✅ OFP généré avec succès !")
            st.download_button(
                label="📥 Télécharger l'OFP complété",
                data=final_pdf,
                file_name=f"OFP_{flight_data.get('flight_number', 'C525')}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
