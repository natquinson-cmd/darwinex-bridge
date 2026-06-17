# 🌉 Pont IG → Darwinex Zero (compte 4000093713)

Réplique automatiquement vos positions IG (l'algo) vers le compte virtuel
Darwinex Zero. Miroir d'événements : IG est la seule source de vérité,
le pont ne décide jamais rien.

**Deux variantes au choix — même logique, mêmes garde-fous :**

| | v1 `bridge_ig_mt5.py` | v2 ☁️ `bridge_ig_metaapi.py` (recommandée) |
|---|---|---|
| Côté MT5 | Terminal MT5 installé sur le PC | **Aucun terminal** — MetaApi.cloud héberge la connexion 24/7 |
| Le script tourne | sur ce PC (Windows) | n'importe où : ce PC, mini-VPS Linux à 4 €… |
| Coût | 0 € | free tier MetaApi, sinon ~5-10 $/mois |

## ☁️ Variante v2 — MetaApi.cloud (sans terminal)

### 1. Créer le compte MetaApi (~5 min)
1. Inscription sur **https://app.metaapi.cloud**
2. Menu → **API tokens** (ou Settings → Token) → générer un token → le copier
3. **Trading accounts → Add account** : type **MT5**, login `4000093713`,
   mot de passe **master** (celui de l'email Darwinex, pas l'investor),
   serveur : celui de l'email (la recherche le propose), région au choix
   → après création, copier l'**Account ID** (UUID affiché sur la fiche du compte)

### 2. Configurer & tester
```powershell
cd C:\Users\quinson\Desktop\Claude\Darwinex_Bridge
pip install metaapi-cloud-sdk
notepad config.json    # remplir ig.* + metaapi.token + metaapi.account_id
python bridge_ig_metaapi.py --selftest
```
Attendu : `✅ SELFTEST OK : IG joignable, MetaApi connecté, symboles mappés.`

### 3. Lancer
```powershell
python bridge_ig_metaapi.py     # dry_run: true au début, comme la v1
```
Journal : `bridge_metaapi.log` · État : `bridge_state_metaapi.json`
Le compte reste visible en direct sur app.metaapi.cloud (positions, équité).

> Plus tard, pour zéro machine chez vous : ce même script se déplace tel quel
> sur un mini-VPS Linux (~4 €/mois) — demandez-moi le guide le moment venu.

## Installation (une seule fois, ~15 min)

### 1. Terminal MT5 Darwinex
- Téléchargez **MetaTrader 5** depuis votre espace Darwinex Zero (ou metatrader5.com)
- Connexion : login `4000093713` + mot de passe + serveur **indiqués dans l'email Darwinex**
- Cochez « Mémoriser le mot de passe », laissez le terminal ouvert
- **Activez le bouton « Algo Trading »** (barre d'outils, doit être vert)

### 2. Python
```powershell
cd C:\Users\quinson\Desktop\Claude\Darwinex_Bridge
pip install MetaTrader5
```

### 3. Configuration
```powershell
copy config.example.json config.json
notepad config.json
```
- `ig.*` : **les mêmes identifiants que le bouton « 🔄 Synchroniser IG » du dashboard**
  (clé API + identifiant + mot de passe IG)
- `mt5.server` : le nom du serveur de l'email Darwinex (ex. `Darwinex-Live`)
- `mt5.password` : **laisser vide** si le terminal est déjà connecté (recommandé) —
  le pont s'attache alors au terminal ouvert
- `dry_run: true` pour l'instant — le pont loggue tout mais n'envoie aucun ordre

> 🔒 `config.json` contient des secrets : il reste sur CE PC, ne le partagez
> jamais, ne le poussez jamais sur GitHub (déjà exclu par .gitignore).

### 4. Test de connexion (sans aucun ordre)
```powershell
python bridge_ig_mt5.py --selftest
```
Attendu : `✅ SELFTEST OK : IG joignable, MT5 connecté, symboles mappés.`

## Démarrage

```powershell
python bridge_ig_mt5.py
```

**Procédure de validation recommandée :**
1. Jour 1-2 : `dry_run: true` — vérifiez dans `bridge.log` que chaque trade IG
   déclenche bien `[DRY-RUN] OUVERTURE …` puis `[DRY-RUN] FERMETURE …`
2. Ensuite : passez `dry_run: false` → les ordres partent réellement sur le
   compte virtuel Darwinex (rappel : capital virtuel, zéro risque réel)

## Lancement automatique en semaine (optionnel)

```powershell
schtasks /Create /TN "PontDarwinex" /TR "python C:\Users\quinson\Desktop\Claude\Darwinex_Bridge\bridge_ig_mt5.py" /SC WEEKLY /D LUN,MAR,MER,JEU,VEN /ST 07:25 /F
```
Le pont s'endort tout seul hors de la fenêtre 07:25–22:20 et le week-end.

## Les garde-fous intégrés

| Garde-fou | Rôle |
|---|---|
| **Stop catastrophe 3 %** | posé sur chaque position MT5 dès l'ouverture — si le pont meurt, la perte virtuelle est bornée |
| **Réconciliation au démarrage** | compare IG / MT5 / fichier d'état et resynchronise (coupure, reboot, stop touché pendant l'arrêt…) |
| **Failsafe 22:05** | ferme tout miroir MT5 dont la position IG n'existe plus (le trade week-end IG, lui, reste ouvert) |
| **Re-login automatique** | session IG expirée → reconnexion sans intervention |
| **Alertes Telegram** | optionnel : remplissez `telegram.bot_token` + `chat_id` pour être prévenu de chaque ouverture/fermeture/erreur |

## Sizing (mode réinvestissement)

IG donne la taille en €/point. Le pont applique le **même €/point ramené à
l'équité du compte MT5** : `taille_MT5 = taille_IG × (équité_MT5 / équité_IG)`.
Les tailles suivent donc le capital — risque relatif constant, VaR stable,
exactement ce que le moteur Darwinex récompense.

## Suivi

- Journal : `bridge.log` (toutes les décisions, horodatées)
- État courant : `bridge_state.json` (mapping positions IG ↔ tickets MT5)
- En cas de doute : `python bridge_ig_mt5.py --once` fait un seul cycle et s'arrête
