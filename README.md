# Debrief BTC (V1)

Outil **Python simple et robuste** pour générer un débrief journalier de ton bot BTC à partir de fichiers d'analyse.

> ⚠️ Cet outil est en **analyse uniquement**: il ne modifie pas le bot de trading, ne passe aucun ordre MT5, ne se connecte à aucun broker.

## Fichiers attendus

Structure recommandée:

```text
DEBRIEF_BTC/
├── debrief_btc.py
├── console.txt
├── snapshots_btc.jsonl
├── mt5_history.csv
├── notes.txt
├── charts/
│   ├── 2026-04-25_09h35_M5.png
│   ├── 2026-04-25_09h35_M15.png
│   └── ...
└── OUTPUT/
```

## Lancer l'outil

Depuis le dossier `DEBRIEF_BTC/`:

```bash
python debrief_btc.py
```

Le script crée automatiquement le dossier `OUTPUT/` si besoin.

## Livrables générés

Dans `OUTPUT/`:

- `rapport_journalier.html` : rapport complet
- `resume_chatgpt.txt` : résumé court copiable dans ChatGPT
- `anomalies.txt` : liste d'alertes / points suspects

## Ce que la V1 analyse

1. **console.txt**
   - détecte les événements clés (`ALLOW`, `BLOCK`, `WAIT_1`, `WAIT_2`, `GPT_TIMEOUT`, `M5_TOO_FAR`, etc.)
   - repère les lignes d'erreur importantes (`error`, `exception`, `traceback`, ...)

2. **snapshots_btc.jsonl**
   - parse ligne par ligne sans planter
   - ignore les JSON invalides et les liste en anomalies
   - extrait les champs disponibles et calcule des stats utiles (skip reasons, décisions GPT, spread moyen, distance M5 moyenne)

3. **mt5_history.csv**
   - détecte automatiquement séparateur et colonnes proches (même si les noms varient)
   - détecte aussi les rapports MT5/PuPrime tabulés (sections `Positions`, `Ordres`, `Transactions`, `Résultats`)
   - dans un rapport tabulé, parse en priorité la section `Positions` (lignes entre `Positions` et `Ordres`)
   - convertit les nombres français (`76 247,00`, `- 15,45`, etc.)
   - ignore les lignes vides/inexploitables (non comptées comme trades)
   - si des lignes non vides existent mais restent non parsables, remonte l'anomalie: `CSV MT5 non reconnu ou colonnes incompatibles`
   - lit `Résultats` comme contrôle (`Nb trades`, `Profit Total Net`) sans remplacer les trades parsés
   - calcule: nombre de trades, TP/SL/BE, net total, moyennes gains/pertes, meilleur/pire trade, horaires, durée moyenne

4. **charts/**
   - lit `.png/.jpg/.jpeg`
   - détecte `M5` / `M15` dans le nom
   - extrait l'heure depuis le nom quand possible
   - associe les captures aux trades proches et les insère dans le HTML

## Robustesse

- Fichier manquant: message clair + génération maintenue
- Fichier vide: pas de crash
- JSON invalide: pas de crash
- Colonnes MT5 variables: mapping flexible
- Timeout GPT fallback ALLOW détecté: remonté en anomalie prioritaire (`DANGER : GPT timeout fallback ALLOW détecté`)
- Section **Skips importants** basée sur les événements console (ENTRY_FILTER_SKIP, M5_TOO_FAR, OUT_OF_SESSION, REENTRY_TOO_CLOSE, GPT_BLOCK, WAIT_1, WAIT_2)

## Notes

- V1 privilégie la fiabilité et la lisibilité, pas un design complexe.
- PDF non inclus en V1 pour rester simple.
