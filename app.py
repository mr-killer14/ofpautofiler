import streamlit as st
import pdfplumber
import re
import io
import textwrap
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import black, red

st.set_page_config(page_title="C525 Smart OFP", layout="wide")

# --- 1. FONCTIONS D'ANALYSE (DONNÉES) ---

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'\|', ' ', text))

def get_best_runway(wind_dir_str, runways_str):
    """Déduit la piste en service en fonction du vent."""
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
            min_diff = diff
            best_rwy = rwy
    return best_rwy

def analyze_weather(text, airport):
    """Analyse le METAR : vent, température, et conditions WET/DRY."""
    oat, wind_dir, condition, runways = None, None, "DRY", ""
    rwy_match = re.search(rf'{airport}.*?RWY\s+([\w\s]+)', text)
    if rwy_match: runways = rwy_match.group(1).strip()
    
    metar_match = re.search(rf'METAR\s+.*?{airport}.*?(\d{{3}}|VRB)(\d{{2,3}})G?\d*KT.*?\s+(M?\d{{2}})/(M?\d{{2}})', text, re.DOTALL)
    if metar_match:
        wind_dir = metar_match.group(1)
        oat = metar_match.group(3).replace('M', '-')
        # Mots-clés pluie/neige/brouillard
        condition = "WET" if re.search(r'(RA|DZ|SN|SHRA|FG|BR|HZ)', metar_match.group(0)) else "DRY"
        
    return oat, get_best_runway(wind_dir, runways), condition

def extract_apg_data(full_text, flight_data):
    """Récupère les perfos APG en fonction de la piste et de l'état (Wet/Dry)."""
    dep = flight_data.get('departure', '')
    dest = flight_data.get('destination', '')
    alt = flight_data.get('alternate', '')
    
    # Takeoff Data
    if dep:
        # Tente de trouver le bloc exact pour la piste et la condition
        to_match = re.search(rf'TAKEOFF PERFORMANCE.*?{dep}.*?(\d{{2}}\.\d)\s+(\d{{4,5}})\s+[A-Z]+\s+[\d/]+\s+(\d{{3,4}})', full_text, re.DOTALL)
        if to_match:
            flight_data['to_power'] = to_match.group(1) + "%"
            flight_data['obst_limit'] = to_match.group(2)
            flight_data['lvl_off'] = to_match.group(3)

    # Escape Route
    escape_match = re.search(r'SPECIAL DEPARTURE PROCEDURES.*?NOTE: NON-RNAV PROCEDURE.*?(?=###)', full_text, re.DOTALL)
    if escape_match:
        cl = escape_match.group(0).replace('b ', '').replace('\n', ' ')
        flight_data['escape_route'] = re.sub(r'\s+', ' ', cl).strip()

    # Landing Data
    max_dest, max_alt = "9900", "9900"
    if dest:
        dest_cond = flight_data.get('dest_cond', 'DRY').upper()
        dest_match = re.search(rf'LANDING PERFORMANCE.*?{dest}.*?COND:\s*{dest_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if dest_match: max_dest = dest_match.group(1)
    if alt:
        alt_cond = flight_data.get('alt_cond', 'DRY').upper()
        alt_match = re.search(rf'LANDING PERFORMANCE.*?{alt}.*?COND:\s*{alt_cond}.*?2\.5%\s+(\d{{4}})', full_text, re.DOTALL | re.IGNORECASE)
        if alt_match: max_alt = alt_match.group(1)
        
    flight_data['max_ldg'] = f"{max_dest}/{max_alt}"

# --- 2. FONCTIONS DE GÉOMÉTRIE DYNAMIQUE ---

def scan_template_anchors(template_bytes):
    """
    Scanne le document PDF vierge mot par mot pour trouver les coordonnées (X,Y)
    exactes des zones où nous devons écrire ou dessiner, peu importe la page.
    """
    anchors = {}
    max_mora = 0
    
    with pdfplumber.open(template_bytes) as pdf:
        for page_num, page in enumerate(pdf.pages):
            words = page.extract_words()
            for i, w in enumerate(words):
                text = w['text'].replace(':', '')
                x1 = w['x1']
                y = 842 - w['top'] # Conversion pour ReportLab (origine en bas à gauche)
                
                # --- Textes à écrire à côté du mot clé (X décalé vers la droite) ---
                if text == "FLT" and "flight_number" not in anchors: anchors["flight_number"] = (page_num, x1 + 5, y)
                elif text == "DEP" and "departure" not in anchors: anchors["departure"] = (page_num, x1 + 5, y)
                elif text == "DEST" and "destination" not in anchors: anchors["destination"] = (page_num, x1 + 5, y)
                elif text == "ALTN" and "alternate" not in anchors: anchors["alternate"] = (page_num, x1 + 5, y)
                elif text == "TOW" and "tow" not in anchors: anchors["tow"] = (page_num, x1 + 5, y)
                elif text == "ZFW" and "zfw" not in anchors: anchors["zfw"] = (page_num, x1 + 5, y)
                
                elif text == "POWER" and i > 0 and "T/O" in words[i-1]['text']: anchors["to_power"] = (page_num, x1 + 5, y)
                elif text == "LIMIT" and i > 0 and "OBST" in words[i-1]['text']: anchors["obst_limit"] = (page_num, x1 + 5, y)
                elif text == "OFF" and i > 0 and "LVL" in words[i-1]['text']: anchors["lvl_off"] = (page_num, x1 + 5, y)
                elif "RMQ" in text and "escape_route" not in anchors: anchors["escape_route"] = (page_num, x1 + 10, y)
                
                elif text == "DISPATCH" and "(DEST/ALTN)" in " ".join([w2['text'] for w2 in words[i:i+7]]):
                    anchors["max_ldg"] = (page_num, x1 + 175, y) # Décalage estimé après la parenthèse
                
                # --- Carburants (Alignement vertical forcé sur colonne de droite) ---
                elif text == "TRIP" and "trip_fuel" not in anchors: anchors["trip_fuel"] = (page_num, 535, y)
                elif text == "CONT" and "cont_fuel" not in anchors: anchors["cont_fuel"] = (page_num, 535, y)
                elif "ALT1" in text and "alt_fuel" not in anchors: anchors["alt_fuel"] = (page_num, 535, y)
                elif "FINAL" in text and "final_fuel" not in anchors: anchors["final_fuel"] = (page_num, 535, y)
                elif "RAMP" in text and i < len(words)-1 and "MREQ" in words[i+1]['text'] and "ramp_fuel" not in anchors: 
                    anchors["ramp_fuel"] = (page_num, 535, y)

                # --- Page 2: Drift Down & MORA ---
                elif "-TOC-" in text and "toc" not in anchors:
                    anchors["toc"] = (page_num, 175, y)
                elif text.isdigit() and 50 <= int(text) <= 250:
                    val = int(text)
                    if val > max_mora:
                        max_mora = val
                        anchors["max_mora_val"] = val
                        anchors["mora"] = (page_num, 425, y)

                # --- Encadrés dynamiques (PF/PM & OPS) ---
                # On récupère la boîte exacte (x0, y, largeur, hauteur) pour encadrer le mot
                elif text in ["COM", "PVT", "TRG", "MED"]:
                    box_w = w['x1'] - w['x0'] + 6
                    anchors[f"box_OPS_{text}"] = (page_num, w['x0'] - 3, y - 2, box_w, 12)
                elif text == "CPT" and "box_CPT" not in anchors:
                    anchors["box_CPT"] = (page_num, w['x0'] - 2, y - 2, w['x1'] - w['x0'] + 4, 12)
                elif "F/O" in text and "box_FO" not in anchors:
                    anchors["box_FO"] = (page_num, w['x0'] - 2, y - 2, w['x1'] - w['x0'] + 4, 12)

    return anchors

def draw_dynamic_pdf(template_bytes, flight_data, anchors):
    """Dessine les données et rectangles sur un PDF en fonction des ancres trouvées."""
    template_reader = PdfReader(template_bytes)
    writer = PdfWriter()
    
    # Nous créons une toile (canvas) par page
    pages_canvases = {}
    
    for page_num in range(len(template_reader.pages)):
        packet = io.BytesIO()
        can = canvas.Canvas(packet, pagesize=A4)
        pages_canvases[page_num] = {"packet": packet, "canvas": can}

    # 1. Dessiner les Textes Standards
    for key, text_val in flight_data.items():
        if key in anchors and text_val:
            page_num, x, y = anchors[key]
            can = pages_canvases[page_num]["canvas"]
            can.setFont("Helvetica-Bold", 10)
            can.setFillColor(black)
            can.drawString(x, y, str(text_val))
            
    # 2. Dessiner l'Escape Route
    if flight_data.get('escape_route') and "escape_route" in anchors:
        page_num, x, y = anchors["escape_route"]
        can = pages_canvases[page_num]["canvas"]
        can.setFillColor(black)
        textobject = can.beginText(x, y)
        textobject.setFont("Helvetica", 8)
        lines = textwrap.wrap(flight_data['escape_route'], width=65)
        for line in lines: textobject.textLine(line)
        can.drawText(textobject)

    # 3. Dessiner la Drift Down & MORA (Rouge)
    if "toc" in anchors and flight_data.get('driftdown_fl'):
        page_num, x, y = anchors["toc"]
        can = pages_canvases[page_num]["canvas"]
        can.setFont("Helvetica-Bold", 9)
        can.setFillColor(red)
        can.drawString(x, y, f"DD: FL {flight_data['driftdown_fl']}")
        
    if "mora" in anchors and "max_mora_val" in anchors:
        page_num, x, y = anchors["mora"]
        can = pages_canvases[page_num]["canvas"]
        can.setFont("Helvetica-Bold", 9)
        can.setFillColor(red)
        recovery_alt = (anchors["max_mora_val"] * 100) + 1000
        can.drawString(x, y, f"Rec: {recovery_alt} FT")

    # 4. Dessiner les Encadrés Rouges (PF/PM & OPS)
    for page_num in pages_canvases:
        can = pages_canvases[page_num]["canvas"]
        can.setStrokeColor(red)
        can.setLineWidth(1.5)
        can.setFillAlpha(0.0) # Transparent
        
        # OPS
        ops_key = f"box_OPS_{flight_data.get('ops', '')}"
        if ops_key in anchors and anchors[ops_key][0] == page_num:
            _, bx, by, bw, bh = anchors[ops_key]
            can.roundRect(bx, by, bw, bh, 3)
            
        # PF/PM (On dessine sous CPT ou F/O)
        if flight_data.get('pf') == "CPT":
            pf_target = "box_CPT"
            pm_target = "box_FO"
        else:
            pf_target = "box_FO"
            pm_target = "box_CPT"

        if pf_target in anchors and anchors[pf_target][0] == page_num:
            # Encadré PF juste en dessous du titre CPT ou F/O
            _, bx, by, bw, bh = anchors[pf_target]
            can.roundRect(bx + 5, by - 12, 20, 10, 2) # Boîte PF
        if pm_target in anchors and anchors[pm_target][0] == page_num:
            # Encadré PM
            _, bx, by, bw, bh = anchors[pm_target]
            can.roundRect(bx + 35, by - 12, 22, 10, 2) # Boîte PM

    # --- FUSION FINALE ---
    for page_num, page in enumerate(template_reader.pages):
        can = pages_canvases[page_num]["canvas"]
        can.save()
        packet = pages_canvases[page_num]["packet"]
        packet.seek(0)
        overlay = PdfReader(packet).pages[0]
        page.merge_page(overlay)
        writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output

# --- 3. INTERFACE WEB (STREAMLIT) ---

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
        st.warning("⚠️ Veuillez charger les documents requis.")
    else:
        with st.spinner("Analyse visuelle du modèle et calculs en cours..."):
            flight_data = {"pf": pf, "ops": ops, "driftdown_fl": driftdown_fl}
            
            # Parsing W&B
            with pdfplumber.open(wb_file) as pdf:
                text = clean_text(pdf.pages[0].extract_text())
                tow = re.search(r'TOW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                zfw = re.search(r'ZFW\s+Max\s+\d+\s+(\d{4,5})\s+OK', text)
                if tow: flight_data['tow'] = tow.group(1)
                if zfw: flight_data['zfw'] = zfw.group(1)

            # Parsing Flight Package & Météo & APG
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
                alt1 = re.search(r'ALT1.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if alt1: flight_data['alt_fuel'] = alt1.group(1)
                fin = re.search(r'FINAL RSRV.*?(?:\d:\d{2}\s+)?(\d{2,4})', clean_full)
                if fin: flight_data['final_fuel'] = fin.group(1)
                ramp = re.search(r'RAMP MREQ\s+(\d{3,5})', clean_full)
                if ramp: flight_data['ramp_fuel'] = ramp.group(1)

                # Météo (OAT, Runway, Condition)
                dep_oat, flight_data['dep_rwy'], dep_cond = analyze_weather(full_text, flight_data.get('departure'))
                _, flight_data['dest_rwy'], flight_data['dest_cond'] = analyze_weather(full_text, flight_data.get('destination'))
                _, flight_data['alt_rwy'], flight_data['alt_cond'] = analyze_weather(full_text, flight_data.get('alternate'))

                # APG
                extract_apg_data(full_text, flight_data)

            # Scanner dynamiquement le modèle vierge pour trouver où écrire
            template_file.seek(0)
            anchors = scan_template_anchors(template_file)
            
            # Dessiner le PDF
            template_file.seek(0)
            final_pdf = draw_dynamic_pdf(template_file, flight_data, anchors)

            st.success("✅ L'OFP a été généré et s'est calé dynamiquement sur l'emplacement des champs de votre modèle !")
            st.download_button(
                label="📥 Télécharger l'OFP complété",
                data=final_pdf,
                file_name=f"OFP_{flight_data.get('flight_number', 'C525')}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
