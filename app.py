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
# Basées sur le modèle PdfMaker_2.pdf
# Origine (0,0) en bas à gauche.
# ==========================================
POSITIONS_P1 = {
    # En-tête
    "flight_number": (60, 814),
    "departure":     (60, 785),
    "destination":   (60, 770),
    "alternate":     (60, 755),
    "tow":           (420, 740),
    
    # Bloc Fuel (Colonne de droite)
    "trip_fuel":     (520, 642),
    "cont_fuel":     (520, 627),
    "alt_fuel":      (520, 612),
    "final_fuel":    (520, 582),
    "ramp_fuel":     (520, 537),
    
    # RMQ / Autres
    "zfw":           (90, 442),
    "to_power":      (100, 420),
    "obst_limit":    (100, 405),
    "lvl_off":       (100, 390),
    "max_ldg":       (300, 260),
}

# Coordonnées des éléments à entourer (X, Y, Largeur, Hauteur)
BOXES = {
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
        
    return oat, wind_dir, condition

def extract_apg_data(full_text, flight_data):
    """Cherche les T/O et LDG perfos dans le texte APG."""
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
        dest_match = re.search(rf'LANDING PERFORMANCE.*?{dest}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if dest_match: max_dest = dest_match.group(1)
    if alt:
        alt_match = re.search(rf'LANDING PERFORMANCE.*?{alt}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if alt_match: max_alt = alt_match.group(1)
        
    flight_data['max_ldg'] = f"{max_dest}/{max_alt}"

def get_dynamic_coordinates(pdf_file):
    """Scanne la page 2 pour coller la Drift Down au TOC et MORA."""
    toc_coords, mora_coords, max_mora = None, None, 0
    try:
        with pdfplumber.open(pdf_file) as pdf:
            if len(pdf.pages) > 1:
                page2 = pdf.pages[1]
                for w in page2.extract_words():
                    if "-TOC-" in w['text']:
                        toc_coords = (w['x1'] + 10, 842 - w['top'])
                    if w['text'].isdigit() and 50 <= int(w['text']) <= 250:
                        if int(w['text']) > max_mora:
                            max_mora = int(w['text'])
                            mora_coords = (w['x1'] + 20, 842 - w['top'])
    except: pass
    return toc_coords, mora_coords, max_mora

def draw_pdf(template_bytes, flight_data, toc_coords, mora_coords, max_mora):
    """Dessine les textes et rectangles sur les pages du PDF."""
    packet1 = io.BytesIO()
    can1 = canvas.Canvas(packet1, pagesize=A4)
    can1.setFont("Helvetica-Bold", 10)
    can1.setFillColor(black)
    
    # 1. Textes P1
    for key, text_val in flight_data.items():
        if key in POSITIONS_P1 and text_val:
            x, y = POSITIONS_P1[key]
            can1.drawString(x, y, str(text_val))
            
    # 2. Escape Route (dessiné comme un bloc de texte)
    if flight_data.get('escape_route'):
        textobject = can1.beginText(320, 420)
        textobject.setFont("Helvetica", 8)
        lines = textwrap.wrap(flight_data['escape_route'], width=60)
        for line in lines: textobject.textLine(line)
        can1.drawText(textobject)

    # 3. Formes Géométriques Rouges (PF/PM et OPS)
    can1.setStrokeColor(red)
    can1.setLineWidth(1.5)
    can1.setFillColor(red)
    can1.setFillAlpha(0.0) # Fond transparent
    
    # Entourer PF/PM
    if flight_data['pf'] == "CPT":
        box_pf = BOXES["PF_CPT"]
        box_pm = BOXES["PM_FO"]
    else:
        box_pm = BOXES["PM_CPT"]
        box_pf = BOXES["PF_FO"]
        
    can1.roundRect(box_pf[0], box_pf[1], box_pf[2], box_pf[3], 4)
    can1.roundRect(box_pm[0], box_pm[1], box_pm[2], box_pm[3], 4)

    # Entourer OPS
    ops_box = BOXES.get(f"OPS_{flight_data['ops']}")
    if ops_box:
        can1.roundRect(ops_box[0], ops_box[1], ops_box[2], ops_box[3], 4)

    can1.save()
    packet1.seek(0)
    
    # --- PAGE 2 ---
    packet2 = io.BytesIO()
    can2 = canvas.Canvas(packet2, pagesize=A4)
    can2.setFont("Helvetica-Bold", 9)
    can2.setFillColor(red)
    
    if toc_coords and flight_data.get('driftdown_fl'):
        can2.drawString(toc_coords[0], toc_coords[1], f"DD: FL{flight_data['driftdown_fl']}")
        
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
        with st.spinner("Analyse des documents en cours..."):
            flight_data = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
            
            # Parsing W&B
            with pdfplumber.open(wb_file) as pdf:
                text = clean_text(pdf.pages[0].extract_text())
                # Capture robuste "ZFW Max 8500 7507 OK"
                tow = re.search(r'TOW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                zfw = re.search(r'ZFW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                if tow: flight_data['tow'] = tow.group(1)
                if zfw: flight_data['zfw'] = zfw.group(1)

            # Parsing Flight Package & APG
            with pdfplumber.open(fp_file) as pdf:
                full_text = " ".join([p.extract_text() for p in pdf.pages if p.extract_text()])
                clean_full = clean_text(full_text)
                
                route = re.search(r'DEP\s+([A-Z]{4}).*?DEST\s+([A-Z]{4}).*?ALTN\s+([A-Z]{4})', clean_full)
                if route:
                    flight_data['departure'] = route.group(1)
                    flight_data['destination'] = route.group(2)
                    flight_data['alternate'] = route.group(3)
                    
                flt = re.search(r'FLT\s+([A-Z0-9]+)', clean_full)
                if flt: flight_data['flight_number'] = flt.group(1)
                
                # Fuel
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

                # Météo + APG Perfos
                dep_oat, _, dep_cond = analyze_weather(full_text, flight_data.get('departure'))
                _, _, flight_data['dest_cond'] = analyze_weather(full_text, flight_data.get('destination'))
                _, _, flight_data['alt_cond'] = analyze_weather(full_text, flight_data.get('alternate'))

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
