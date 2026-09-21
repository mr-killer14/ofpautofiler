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
        Tu es un dispatcher aéronautique C525. Tu dois faire une analyse croisée rigoureuse entre la météo et les tableaux APG.

        MÉTHODE STRICTE DE DÉDUCTION :

        1. DÉCOLLAGE (DEP) :
           a. Lis le METAR du terrain de départ : identifie le vent (direction/vitesse) et la température OAT exacte (ex: 28/18 -> OAT = 28°C).
           b. Détermine la piste en service préférentielle face au vent (ex: piste 06 face au vent 090°).
           c. Vérifie l'état de surface : DRY s'il n'y a pas de pluie/averse/neige, WET si précipitations.
           d. Recherche dans le Flight Package le tableau APG TAKEOFF PERFORMANCE pour cet aéroport, cette piste et cet état (ex: 06DP DRY).
           e. Dans ce tableau, repère la ligne correspondant STRICTEMENT à la température OAT trouvée (ex: ligne 28) :
              - to_power : valeur de la colonne PWR (ex: 98.2)
              - limiting_weight : valeur numérique de LIMIT WT (ex: 10700)
              - obst_or_st_limiting : le code à côté de la masse (ex: ST ou OBST)
              - lvl_off : valeur de la colonne LVLOFF (ex: 1726)

        2. MASSES RÉELLES (W&B) :
           - tow : Take-Off Weight RÉEL lu sur la loadsheet/WB (arrondi entier, ex: 10382)
           - zfw : Zero Fuel Weight RÉEL (arrondi entier, ex: 7507)
           - landing_weight : Landing Weight RÉEL estimé (arrondi entier, ex: 8738)

        3. ATTERRISSAGE (DEST / ALTN) :
           a. Identifie l'OACI de DEST et de l'ALTN dans le Flight Package.
           b. Lis leurs METAR/TAF respectifs : détermine si les pistes seront DRY ou WET à l'arrivée.
           c. Trouve les sections "APG LANDING PERFORMANCE" pour DEST puis pour ALTN.
           d. Relève la masse maximale autorisée à l'atterrissage (Field limit ou Climb limit, souvent plafonnée à 9900 lbs max structural C525 si la piste est longue).
           e. max_ldg : écris-le sous la forme "DEST_VAL/ALTN_VAL" (ex: "9900/9900").

        DOCUMENTS COMPLETS (MÉTÉO + TABLEAUX APG) :
        {fp_text}

        WEIGHT & BALANCE :
        {wb_text}

        Réponds UNIQUEMENT avec cet objet JSON :
        {{
            "reasoning": "Détaille : METAR DEP vent/OAT -> Piste choisie -> Ligne APG lue (PWR, LIMIT, LVLOFF) -> Analyse METAR DEST/ALTN et masses d'atterrissage APG",
            "tow": "10382",
            "zfw": "7507",
            "landing_weight": "8738",
            "to_power": "98.2",
            "obst_or_st_limiting": "ST",
            "limiting_weight": "10700",
            "lvl_off": "1726",
            "escape_route": "Texte de la special departure procedure si présente",
            "max_ldg": "9900/9900"
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
