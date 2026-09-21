import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import json
import io
import textwrap
import google.generativeai as genai

st.set_page_config(page_title="C525 Smart OFP", layout="wide")

# ==========================================
# 1. FONCTIONS D'INTELLIGENCE ARTIFICIELLE
# ==========================================

def extract_data_with_ai(fp_text, wb_text, api_key):
    """Envoie le texte brut à l'IA Gemini en ciblant la dernière version."""
    genai.configure(api_key=api_key)
    
    try:
        # Forçage du modèle explicitement demandé par l'API
        model = genai.GenerativeModel("gemini-3.6-flash")
        
        prompt = f"""
        Tu es un dispatcher aéronautique expert. Analyse ces documents de vol brut (Flight Package contenant Météo et Perfos APG, et une Loadsheet).
        Extrais les informations exactes demandées en format JSON pur.
        
        Règles strictes:
        - tow : masse au décollage RÉELLE (pas la structurelle Max 10700).
        - zfw : zero fuel weight RÉEL (pas la structurelle Max 8500).
        - landing_weight : landing weight RÉEL (pas la structurelle Max 9900).
        - to_power : la puissance de décollage (ex: 98.6%) correspondant à la température OAT du METAR de départ et à la bonne piste.
        - obst_limit : la masse limite d'obstacle APG pour le décollage.
        - lvl_off : l'altitude de Level Off APG en cas de panne moteur.
        - runway : la piste de décollage utilisée dans l'APG.
        - escape_route : le texte de la SPECIAL DEPARTURE PROCEDURE (NOTE: NON-RNAV PROCEDURE...).
        - max_ldg : sous la forme "DEST/ALTN" (ex: "9900/9900"), en utilisant les perfos APG "LANDING PERFORMANCE" adaptées aux METAR (Dry ou Wet selon la pluie).
        
        TEXTE FLIGHT PACKAGE & APG :
        {fp_text[:8000]}
        
        TEXTE WEIGHT & BALANCE :
        {wb_text[:2000]}
        
        Réponds UNIQUEMENT avec ce format JSON (aucune autre phrase) :
        {{
            "tow": "", "zfw": "", "landing_weight": "", "to_power": "",
            "obst_limit": "", "lvl_off": "", "runway": "", "escape_route": "", "max_ldg": ""
        }}
        """
        
        response = model.generate_content(prompt)
        json_str = response.text.replace("```json", "").replace("
