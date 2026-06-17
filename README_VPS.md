# 🖥️ Installer le pont sur un VPS Windows

Objectif : faire tourner le pont 24/7 sur un serveur loué, **sans plus jamais
dépendre de votre PC**. Une fois en place, le VPS lance MT5 + le pont tout seul
à chaque démarrage, en instance unique et invisible.

---

## ⚠️ RÈGLE D'OR — une seule machine en mode réel à la fois

Le verrou anti-doublon protège **à l'intérieur d'une machine**, pas entre deux
machines. Donc : le jour où le VPS passe en réel (`dry_run: false`), **arrêtez
le pont de votre PC** (sinon PC + VPS ouvriraient chacun la position = doublon).
- Sur le PC : supprimez le raccourci `PontDarwinex.lnk` du dossier Démarrage
  et fermez la fenêtre/processus du pont. (Ou laissez le PC en `dry_run: true`.)

---

## Étape 1 — Commander le VPS
Voir le comparatif fourni séparément. Au moment de commander :
- **Windows Server** 2022 (souvent un modèle « Forex » avec MT5 pré-installé)
- **4 Go de RAM** minimum, 2 vCPU, ~40 Go disque (largement suffisant)
- **Localisation Europe** (Allemagne / Royaume-Uni / France) — proche d'IG/Darwinex
- Notez l'**adresse IP**, le **login** et le **mot de passe** RDP reçus par email

## Étape 2 — Se connecter en RDP
Sur votre PC : touche Windows → tapez **« Connexion Bureau à distance »** →
entrez l'IP, le login, le mot de passe du VPS. Vous voyez le bureau du serveur.

## Étape 3 — Installer les pré-requis sur le VPS
1. **Python 3.12** : https://www.python.org/downloads/windows/ →
   ⚠️ cocher **« Add python.exe to PATH »** à l'installation
2. **MetaTrader 5 Darwinex** : si pas déjà présent, téléchargez-le depuis votre
   espace Darwinex. Connectez-vous au compte **4000093713** (mot de passe + serveur
   `Darwinex-Live`), cochez **« Conserver le mot de passe »**, puis activez le
   bouton **« Algo Trading »** (vert).

## Étape 4 — Copier le dossier du pont
Copiez tout le dossier **`Darwinex_Bridge`** de votre PC vers le VPS
(glisser-déposer fonctionne dans la fenêtre RDP, ou via un partage/clé).
Il contient déjà `config.json` avec vos identifiants — rien à ressaisir.

## Étape 5 — Lancer l'installation
Sur le VPS, double-cliquez **`setup_vps.bat`**. Il installe la dépendance,
vérifie la config et crée la tâche planifiée (démarrage auto, instance unique,
redémarrage auto, sans fenêtre).

## Étape 6 — Démarrer
Dans PowerShell sur le VPS :
```powershell
Start-ScheduledTask -TaskName PontDarwinex
```
Vérifiez le journal :
```powershell
Get-Content "$HOME\...\Darwinex_Bridge\bridge_metaapi.log" -Tail 10   # ou bridge.log
```
Vous devez voir `🔒 Verrou unique acquis` puis les connexions IG + MT5.

## Étape 7 — Quitter proprement
**Déconnectez** la session RDP (croix de la fenêtre) — **ne PAS « Fermer la
session / Log off »** : la session doit rester active pour que MT5 continue de
tourner. À la reconnexion, tout est resté en marche.

---

## Bascule en réel
Quand le dry-run du VPS est validé : sur le VPS, mettez `"dry_run": false` dans
`config.json`, relancez la tâche (`Restart-ScheduledTask -TaskName PontDarwinex`),
et **arrêtez le pont du PC** (voir Règle d'or). La calibration Darwinex démarre.

## Dépannage
| Symptôme | Cause / solution |
|---|---|
| Log : `Échec connexion MT5` | Terminal MT5 pas lancé/connecté — ouvrez-le, loguez le compte, « Conserver le mot de passe » |
| Ordres refusés | Bouton « Algo Trading » pas activé (doit être vert) |
| `⛔ Un autre pont tourne déjà` | Normal si déjà lancé — un seul tourne, c'est le but |
| Rien dans le log après reboot | La tâche démarre **à l'ouverture de session** : il faut être connecté en RDP au moins une fois (ou configurer l'auto-login Windows du VPS) |
