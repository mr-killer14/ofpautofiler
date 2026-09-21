import streamlit as st
import pdfplumber
import re
import io
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import black, red
from PIL import Image
import pytesseract

# ==========================================
# COORDONNÉES EXACTES (X, Y) EN POINTS
# ==========================================
POSITIONS_P1 = {
    "flight_number": (75, 785),
    "departure":     (85, 765),
    "destination":   (85, 750),
    "alternate":     (85, 735),
    "tow":           (475, 715),
    "trip_fuel":     (535, 595),
    "cont_fuel":     (535, 580),
    "alt_fuel":      (535, 565),
    "final_fuel":    (535, 550),
    "ramp_fuel":     (535, 505),
    "zfw":           (85, 475),
    "to_power":      (150, 435),
    "obst_limit":    (150, 405),
    "lvl_off":       (150, 390),
    "max_ldg":       (315, 275),
}

BOXES = {
    "PF_CPT": (85, 680, 105, 692),
    "PM_CPT": (110, 680, 130, 692),
    "PF_FO":  (155, 680, 175, 692),
    "PM_FO":  (180, 680, 200, 692),
    "OPS_COM": (355, 738, 385, 750),
    "OPS_PVT": (388, 738, 415, 750),
    "OPS_TRG": (418, 738, 445, 750),
    "OPS_MED": (448, 738, 475, 750),
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
    oat, wind_dir, condition, runways = None, None, "DRY", ""
    rwy_match = re.search(rf'{airport}.*?RWY\s+([\w\s]+)', text)
    if rwy_match: runways = rwy_match.group(1).strip()
    
    metar_match = re.search(rf'METAR\s+.*?{airport}.*?(\d{{3}}|VRB)(\d{{2,3}})G?\d*KT.*?\s+(M?\d{{2}})/(M?\d{{2}})', text, re.DOTALL)
    if metar_match:
        wind_dir = metar_match.group(1)
        oat = metar_match.group(3).replace('M', '-')
        condition = "WET" if re.search(r'(RA|DZ|SN|SHRA|FG|BR|HZ)', metar_match.group(0)) else "DRY"
        
    best_rwy = get_best_runway(wind_dir, runways)
    return oat, best_rwy, condition

def extract_apg_data(full_text, flight_data):
    dep = flight_data.get('departure', '')
    dest = flight_data.get('destination', '')
    alt = flight_data.get('alternate', '')
    dep_rwy = flight_data.get('dep_rwy', '')
    dest_cond = flight_data.get('dest_cond', 'DRY').upper()
    alt_cond = flight_data.get('alt_cond', 'DRY').upper()
    
    if dep:
        to_match = re.search(rf'TAKEOFF PERFORMANCE.*?{dep}.*?Runway\s+{dep_rwy}.*?(\d{{2}}\.\d)\s+(\d{{4,5}})\s+[A-Z]+\s+[\d/]+\s+(\d{{3,4}})', full_text, re.DOTALL | re.IGNORECASE)
        if not to_match:
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
        dest_match = re.search(rf'LANDING PERFORMANCE.*?{dest}.*?COND:\s*{dest_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if dest_match: max_dest = dest_match.group(1)
    if alt:
        alt_match = re.search(rf'LANDING PERFORMANCE.*?{alt}.*?COND:\s*{alt_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if alt_match: max_alt = alt_match.group(1)
        
    flight_data['max_ldg'] = f"{max_dest}/{max_alt}"

def get_dynamic_coordinates(pdf_file):
    toc_coords, mora_coords, max_mora = None, None, 0
    try:
        with pdfplumber.open(pdf_file) as pdf:
            if len(pdf.pages) > 1:
                page2 = pdf.pages[1]
                for w in page2.extract_words():
                    if "-TOC-" in w['text']:
                        toc_coords = (w['x1'] + 5, 842 - w['top'])
                    if w['text'].isdigit() and 50 <= int(w['text']) <= 250:
                        if int(w['text']) > max_mora:
                            max_mora = int(w['text'])
                            mora_coords = (w['x1'] + 20, 842 - w['top'])
    except: pass
    return toc_coords, mora_coords, max_mora

def draw_pdf(template_bytes, flight_data, toc_coords, mora_coords, max_mora):
    packet1 = io.BytesIO()
    can1 = canvas.Canvas(packet1, pagesize=A4)
    can1.setFont("Helvetica-Bold", 10)
    can1.setFillColor(black)
    
    for key, text_val in flight_data.items():
        if key in POSITIONS_P1 and text_val:
            x, y = POSITIONS_P1[key]
            can1.drawString(x, y, str(text_val))
            
    if flight_data.get('escape_route'):
        import textwrap
        textobject = can1.beginText(320, 435)
        textobject.setFont("Helvetica", 8)
        lines = textwrap.wrap(flight_data['escape_route'], width=55)
        for line in lines: textobject.textLine(line)
        can1.drawText(textobject)

    can1.setStrokeColor(red)
    can1.setLineWidth(2)
    if flight_data['pf'] == "CPT":
        can1.circle(95, 686, 12)
        can1.circle(190, 686, 12)
    else:
        can1.circle(120, 686, 12)
        can1.circle(165, 686, 12)

    ops_box = BOXES.get(f"OPS_{flight_data['ops']}")
    if ops_box:
        can1.roundRect(ops_box[0], ops_box[1], ops_box[2]-ops_box[0], ops_box[3]-ops_box[1], 4)

    can1.save()
    packet1.seek(0)
    
    packet2 = io.BytesIO()
    can2 = canvas.Canvas(packet2, pagesize=A4)
    can2.setFont("Helvetica-Bold", 9)
    can2.setFillColor(black)
    
    if toc_coords and flight_data.get('driftdown_fl'):
        can2.drawString(toc_coords[0], toc_coords[1], f"DD: FL{flight_data['driftdown_fl']}")
        
    if mora_coords:
        recovery_alt = (max_mora * 100) + 1000
        can2.drawString(mora_coords[0], mora_coords[1], f"Rec: {recovery_alt} FT")
        
    can2.save()
    packet2.seek(0)

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

# --- INTERFACE UTILISATEUR STREAMLIT ---

st.title("✈️ C525 Smart OFP Assistant")
st.markdown("Interface optimisée pour iPad. Générez votre OFP automatiquement.")

col1, col2 = st.columns(2)

with col1:
    st.subheader("📁 Documents de vol")
    fp_file = st.file_uploader("1. Flight Package (PDF)", type="pdf")
    wb_file = st.file_uploader("2. Weight & Balance (PDF)", type="pdf")
    template_file = st.file_uploader("3. Modèle OFP Vierge (PDF)", type="pdf")

with col2:
    st.subheader("⚙️ Paramètres du vol")
    pf = st.radio("Pilot Flying (PF) :", ["CPT", "FO"], horizontal=True)
    ops = st.radio("Type of Ops :", ["COM", "PVT", "TRG", "MED"], horizontal=True)
    
    # Remplacement de l'image Drift Down par un champ texte direct pour éviter l'OCR sur serveur
    driftdown_fl = st.text_input("Drift Down FL (Optionnel) :", placeholder="ex: 220")

if st.button("🚀 Analyser & Générer l'OFP", type="primary", use_container_width=True):
    if not (fp_file and wb_file and template_file):
        st.error("Veuillez charger le Flight Package, le Weight & Balance et le modèle vierge.")
    else:
        with st.spinner("Analyse APG, Météo et fusion en cours..."):
            flight_data = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
            
            # Parsing W&B
            with pdfplumber.open(wb_file) as pdf:
                text = clean_text(pdf.pages[0].extract_text())
                tow = re.search(r'TOW\s+(?:Max\s*\d+\s+)?(?:OK\s+)?(\d{4,5})', text)
                zfw = re.search(r'ZFW\s+(?:Max\s*\d+\s+)?(?:OK\s+)?(\d{4,5})', text)
                if tow: flight_data['tow'] = tow.group(1)
                if zfw: flight_data['zfw'] = zfw.group(1)

            # Parsing Flight Package
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
                
                trip = re.search(r'TRIP\s+(?:\d:\d{2}\s+)?(\d{3,5})', clean_full)
                if trip: flight_data['trip_fuel'] = trip.group(1)
                cont = re.search(r'CONT 5%\s+(\d{1,4})', clean_full)
                if cont: flight_data['cont_fuel'] = cont.group(1)
                alt = re.search(r'ALT1.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if alt: flight_data['alt_fuel'] = alt.group(1)
                fin = re.search(r'FINAL RSRV.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if fin: flight_data['final_fuel'] = fin.group(1)
                ramp = re.search(r'RAMP MREQ\s+(\d{3,5})', clean_full)
                if ramp: flight_data['ramp_fuel'] = ramp.group(1)

                # Météo et APG
                dep_oat, flight_data['dep_rwy'], dep_cond = analyze_weather(full_text, flight_data.get('departure'))
                _, flight_data['dest_rwy'], flight_data['dest_cond'] = analyze_weather(full_text, flight_data.get('destination'))
                _, flight_data['alt_rwy'], flight_data['alt_cond'] = analyze_weather(full_text, flight_data.get('alternate'))

                extract_apg_data(full_text, flight_data)

            # Coordonnées géométriques sur le modèle
            toc_coords, mora_coords, max_mora = get_dynamic_coordinates(template_file)

            # Dessin
            template_file.seek(0)
            final_pdf = draw_pdf(template_file, flight_data, toc_coords, mora_coords, max_mora)

            st.success("✅ OFP généré avec succès ! Les données météo, performances et trajectoires ont été appliquées.")
            
            st.download_button(
                label="📥 Télécharger l'OFP complété",
                data=final_pdf,
                file_name=f"OFP_{flight_data.get('flight_number', 'C525')}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
