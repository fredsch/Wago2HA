# Wago2HA

Passerelle entre un automate **Wago 750-881** (programmé avec le logiciel CODESYS Calaos) et **Home Assistant**, via **MQTT** et l'auto-découverte.

L'automate continue d'assurer le pilotage électrique bas niveau ; Wago2HA expose ses entrées/sorties à Home Assistant pour que toute la logique (scénarios, automatisations) soit gérée dans HA.

> Projet sous licence **GPLv3**, dérivé du protocole de [Calaos](https://github.com/calaos/calaos_base). Non affilié à Calaos.

---

## Sommaire

- [Architecture](#architecture)
- [Le protocole Wago ↔ Calaos (reconstitué)](#le-protocole-wago--calaos-reconstitué)
- [Fonctionnalités](#fonctionnalités)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration](#configuration)
- [Suspendre le programme de l'automate](#suspendre-le-programme-de-lautomate)
- [Publier le projet sur GitHub depuis Windows](#publier-le-projet-sur-github-depuis-windows)
- [Limites et points à valider](#limites-et-points-à-valider)

---

## Architecture

```
┌──────────────────────────┐   Modbus TCP (502)   ┌───────────────┐   MQTT    ┌──────────────────┐
│  Wago 750-881            │◄────────────────────►│               │◄─────────►│  Broker MQTT     │
│  + programme CODESYS     │                      │   Wago2HA     │           │  (Mosquitto)     │
│    Calaos                │   UDP Calaos (4646)  │   (Docker)    │           └────────┬─────────┘
│  + 750-1405 / 1504 /     │◄────────────────────►│               │                    │ auto-discovery
│    641 / 640 ...         │                      └───────────────┘                    ▼
└──────────────────────────┘                                                  ┌──────────────────┐
                                                                              │  Home Assistant  │
                                                                              └──────────────────┘
```

Deux canaux vers l'automate :
- **Modbus TCP** : lecture des entrées, écriture/relecture des sorties, lecture des registres analogiques.
- **UDP** (protocole Calaos) : l'automate *pousse* les changements d'état des boutons (pas de polling), et reçoit les commandes DALI.

---

## Le protocole Wago ↔ Calaos (reconstitué)

Établi par lecture du code source `calaos_base/src/bin/calaos_server/IO/Wago`. Ces conventions sont les valeurs **par défaut** de Wago2HA ; elles restent configurables car la cartographie réelle dépend du programme CODESYS chargé dans votre automate.

### Modbus TCP (port 502)

| Opération | Fonction Modbus | Adresse |
|---|---|---|
| Lire une entrée TOR (bouton) | FC01 `read_coils` | `var` |
| Écrire une sortie TOR (relais) | FC05 `write_coil` | `var + 4096` |
| Relire l'état d'une sortie | FC01 `read_coils` | `var + 512` (0x200) |
| Lire un registre analogique | FC03 `read_holding_registers` | `var` |

- `4096` = constante Calaos `WAGO_841_START_ADDRESS` (famille 750-841/881).
- Température PT1000 (750-640) : valeur = `(int16) brut / 10.0` °C, puis correction linéaire optionnelle `a·x + b`.
- Analogique générique : `valeur = brut · coeff_a + coeff_b`.

### UDP (protocole Calaos, port 4646 par défaut)

**Entrant** (automate → passerelle), à chaque changement d'état d'entrée :
```
WAGO INT <numéro_entrée> <état>      # entrée standard
WAGO KNX <numéro_entrée> <état>      # entrée KNX
```
Découverte : l'automate émet `CALAOS_DISCOVER`, la passerelle répond `CALAOS_IP <ip>`.

**Sortant** (passerelle → automate), pilotage DALI via 750-641 :
```
WAGO_DALI_SET <ligne> <groupe> <adresse> <valeur 0-100> <fade 1-10>
WAGO_DALI_GET <ligne> <adresse>
```

### Détection des gestes

Comme dans Calaos, les gestes ne sont **pas** calculés par l'automate mais par la passerelle, à partir des fronts d'entrée :

- **Appui long** : front montant → fenêtre de 500 ms. Relâché avant → `single` ; maintenu ≥ 500 ms → `long`.
- **Multi-clic** : premier front → fenêtre de 500 ms ; on compte les fronts montants. À l'expiration : 1 → `single`, 2 → `double`, ≥ 3 → `triple`.

### Volets

Deux sorties TOR (montée/descente). La passerelle pilote le moteur pendant la durée configurée (`time_up` / `time_down` en secondes) et estime la position en %. Sécurité : jamais les deux relais actifs simultanément, avec un temps mort d'inversion de 0,3 s.

---

## Fonctionnalités

| Équipement | Module Wago | Entité Home Assistant |
|---|---|---|
| Bouton simple | 750-1405 / 430 | `binary_sensor` |
| Bouton clic/appui long | 750-1405 / 430 | `event` (single, long) |
| Bouton simple/double/triple | 750-1405 / 430 | `event` (single, double, triple) |
| Relais / sortie | 750-1504 / 430 | `switch` ou `light` |
| Volet (avec position) | 750-1504 / 430 | `cover` |
| Luminaire DALI gradable | 750-641 | `light` (luminosité) |
| Luminaire DALI RGB | 750-641 | `light` (RGB + luminosité) |
| Capteur de présence DALI | 750-641 | `binary_sensor` (occupancy) |
| Capteur de luminosité DALI | 750-641 | `sensor` (illuminance) |
| Température PT1000 | 750-640 | `sensor` (température) |
| Valeur analogique générique | 750-640 / ... | `sensor` |

Les valeurs analogiques sont lues périodiquement (120 s par défaut, configurable). Les états des boutons arrivent en temps réel par UDP.

---

## Prérequis

- Un automate Wago 750-881 avec le programme CODESYS Calaos qui expose les E/S en Modbus et émet les événements UDP.
- Un broker MQTT (par ex. l'add-on **Mosquitto** de Home Assistant).
- L'intégration **MQTT** activée dans Home Assistant (l'auto-découverte fait le reste).
- Docker (et idéalement Docker Compose).

---

## Installation

```bash
git clone https://github.com/<votre-utilisateur>/Wago2HA.git
cd Wago2HA
mkdir -p config
cp config.example.yaml config/config.yaml
# éditez config/config.yaml
docker compose up -d --build
docker compose logs -f
```

> `network_mode: host` (dans `docker-compose.yml`) est recommandé : il permet à l'automate de joindre le port UDP de la passerelle et à celle-ci de répondre au broadcast `CALAOS_DISCOVER`. Sur Windows/macOS où `host` n'est pas supporté, exposez explicitement `4646/udp` et fixez `udp_listen_addr` ainsi que l'IP cible.

Sans Docker :
```bash
pip install -r requirements.txt
python -m wago2ha config/config.yaml
```

---

## Configuration

Tout se décrit dans `config/config.yaml`. Voir `config.example.yaml` pour un exemple complet et commenté de chaque type d'équipement. Les champs `var`, `var_up`, `address`, etc. correspondent aux adresses utilisées dans votre programme CODESYS Calaos.

Après démarrage, les entités apparaissent automatiquement dans Home Assistant sous l'appareil **Wago2HA**.

---

## Suspendre le programme de l'automate

Vous souhaitez que, lorsque la passerelle tourne, la **logique** du programme automate soit suspendue (pour que Home Assistant devienne le seul cerveau, sans que l'automate ne déclenche ses propres scénarios).

Important : la couche d'E/S Modbus/UDP de l'automate doit **rester active** — sinon la passerelle ne peut plus lire/écrire. Il ne s'agit donc pas d'arrêter la tâche CODESYS, mais de neutraliser la partie « logique » de votre programme.

Deux approches possibles, à choisir selon votre programme :

1. **Bobine « mode distant » (recommandé)** — ajoutez dans votre programme CODESYS une variable booléenne (ex. `bRemoteMode`) mappée sur une bobine Modbus. Quand elle vaut `True`, votre programme saute l'exécution de ses règles internes et se contente de relayer les E/S. Renseignez alors dans la config :
   ```yaml
   suspend_plc_program: true
   suspend_coil: 5000   # adresse de la bobine bRemoteMode
   ```
   Wago2HA mettra cette bobine à `True` au démarrage.

2. **Arrêt de la tâche via CODESYS** — techniquement possible avec le protocole CODESYS, mais cela coupe aussi les E/S : non recommandé ici.

Comme je ne connais pas le détail de votre programme CODESYS, dites-moi quelle approche vous voulez (ou envoyez-moi la structure de votre programme) et j'adapte le code en conséquence. Par défaut, cette fonction est désactivée.

---

## Publier le projet sur GitHub depuis Windows

### 1. Installer les outils (une seule fois)
- **Git pour Windows** : https://git-scm.com/download/win (installez avec les options par défaut).
- Un compte sur https://github.com.

### 2. Créer le dépôt sur GitHub
1. Connectez-vous à GitHub → bouton **New** (ou https://github.com/new).
2. Nom du dépôt : `Wago2HA`. Laissez-le **vide** (ne cochez ni README, ni .gitignore, ni licence — ils sont déjà dans le projet).
3. Cliquez **Create repository**.

### 3. Récupérer le projet sur votre PC
Décompressez l'archive `Wago2HA` que je vous ai fournie, par exemple dans `C:\Users\<vous>\Wago2HA`.

### 4. Envoyer le code (dans PowerShell)
Ouvrez **PowerShell**, puis :
```powershell
cd C:\Users\<vous>\Wago2HA

git init
git add .
git commit -m "Premiere version de Wago2HA"
git branch -M main
git remote add origin https://github.com/<votre-utilisateur>/Wago2HA.git
git push -u origin main
```

> Au premier `git push`, une fenêtre s'ouvre pour vous connecter à GitHub dans le navigateur : connectez-vous et autorisez. Git mémorisera ensuite vos identifiants.
>
> Si Git vous demande votre nom/email la première fois :
> ```powershell
> git config --global user.name "Votre Nom"
> git config --global user.email "vous@example.com"
> ```

### 5. Modifications ultérieures
Après chaque changement :
```powershell
git add .
git commit -m "Description du changement"
git push
```

### (Optionnel) Construire et publier l'image Docker
Si vous voulez une image prête à l'emploi sur GitHub Container Registry, je peux vous fournir un workflow GitHub Actions (`.github/workflows/docker.yml`) qui build et publie l'image automatiquement à chaque push. Demandez-le-moi.

---

## Limites et points à valider

Ce code implémente fidèlement le protocole reconstitué depuis Calaos, mais il **doit être validé sur votre matériel réel**. Points à vérifier en particulier :

- **Offsets Modbus** (`output_write_offset` / `output_read_offset`) : confirmés pour la famille 750-841/881 d'après le source Calaos, mais la cartographie exacte dépend de votre programme CODESYS. Vérifiez qu'écrire une sortie l'actionne bien physiquement.
- **Capteurs DALI (présence / luminosité)** : Calaos n'expose pas explicitement la lecture de capteurs DALI via le 750-641 dans le code consulté. La lecture proposée s'appuie sur `WAGO_DALI_GET` et devra probablement être adaptée au format réel renvoyé par votre programme.
- **Port UDP** : 4646 par défaut (port Calaos historique). Ajustez `udp_listen_port` / `udp_plc_port` selon votre configuration.
- **Format de l'état booléen UDP** : la passerelle accepte `true/false` et `1/0`.

N'hésitez pas à activer les logs détaillés (`LOG_LEVEL=DEBUG`) pour observer les trames pendant la mise au point.
```bash
docker compose run -e LOG_LEVEL=DEBUG wago2ha
```
