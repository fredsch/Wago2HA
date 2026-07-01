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

Le protocole complet et vérifié (extrait du programme automate `wago_881.pro`) est documenté dans **[`docs/WAGO_PROTOCOL.md`](docs/WAGO_PROTOCOL.md)**. Résumé :

**Entrant** (automate → passerelle), à chaque changement d'état d'une entrée TOR :
```
WAGO INT <numéro_entrée> <0|1>
```
Seules les entrées **numériques** sont poussées en UDP ; les valeurs analogiques se lisent en Modbus.

**Sortant** (passerelle → automate) :
```
WAGO_HEARTBEAT                                          # suspend la logique automate
WAGO_SET_SERVER_IP a.b.c.d                              # route les événements d'entrées
WAGO_SET_OUTPUT <idx> <0|1>                             # forçage de sortie (repli)
WAGO_DALI_SET <ligne> <groupe> <adresse> <0-100> <fade> # gradation DALI
WAGO_INFO_VOLET_GET <idx> / WAGO_INFO_VOLET_SET <idx> <pos>  # position volet
```
La passerelle annonce son IP à l'automate au démarrage (`WAGO_SET_SERVER_IP`), il n'y a pas de phase de découverte à gérer côté automate.

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
| Température PT1000 | 750-640 | `sensor` (température) |
| Valeur analogique générique | 750-640 / ... | `sensor` |
| Programme Calaos (version) | — | `sensor` (diagnostic) |
| État de l'automate Wago | — | `binary_sensor` connectivity (diagnostic) |

Les valeurs analogiques sont lues périodiquement (120 s par défaut, configurable). Les états des boutons arrivent en temps réel par UDP.

### Statut et version de l'automate

Deux entités de diagnostic sont créées automatiquement (rattachées à l'appareil « Wago2HA ») :

- **Programme Calaos** : la version du programme installé sur l'automate (ex. `2.3`), avec le type de module en attribut (ex. `750-841`). Récupérée via `WAGO_GET_VERSION`.
- **Automate Wago** : `connectivity` **Online/Offline**, distinct de la disponibilité de la passerelle. La passerelle sonde l'automate (ping UDP `WAGO_GET_VERSION`) toutes les `status_interval_s` secondes : une réponse ⇒ Online, un timeout ⇒ Offline.

À noter la différence entre les deux niveaux de disponibilité : si la **passerelle** s'arrête, toutes les entités passent *unavailable* dans HA (via le testament MQTT). Si la passerelle tourne mais que l'**automate** est injoignable, l'entité « Automate Wago » passe à *Offline* tandis que la passerelle reste disponible.

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

Lorsque la passerelle tourne, on veut que la **logique** du programme automate soit suspendue, pour que Home Assistant devienne le seul cerveau sans que l'automate ne déclenche ses propres scénarios (télérupteurs, volets…).

Bonne nouvelle : **c'est natif dans le programme CODESYS Calaos, aucune modification n'est nécessaire.**

Le programme automate maintient un `HEARTBEAT_TIMER` de 30 s. À chaque trame UDP `WAGO_HEARTBEAT` reçue, il le rearme et passe `HEARTBEAT = TRUE`. Dans cet état :

- il **n'exécute plus** sa logique locale (`ManageOutput`) ;
- il pilote ses sorties physiques **uniquement** depuis l'image réseau `netOutStandard` (écrite par Modbus).

Wago2HA envoie ce heartbeat automatiquement (toutes les 10 s par défaut). Il suffit donc que le service tourne. C'est piloté par la config :

```yaml
plc:
  heartbeat: true            # maintient l'automate en mode distant (logique suspendue)
  heartbeat_interval_s: 10   # < 30 s impératif
```

Conséquences importantes :

- Si Wago2HA s'arrête, l'automate **repasse en mode autonome après 30 s** et reprend sa propre logique : la maison continue de fonctionner (repli gracieux). Vous pouvez régler ce comportement de repli via les commandes `WAGO_SET_OUTTYPE`/`WAGO_SET_OUTADDR` (voir `docs/WAGO_PROTOCOL.md`).
- Les écritures de sorties (Modbus) ne sont prises en compte par l'automate **que** tant que le heartbeat est maintenu. C'est pourquoi `heartbeat` doit rester activé en fonctionnement normal.

> L'ancienne approche par « bobine `bRemoteMode` » (clés `suspend_plc_program`/`suspend_coil`) est **abandonnée** : elle est inutile puisque le heartbeat fait le travail nativement. Ces clés sont ignorées si elles subsistent dans une ancienne config.

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

---

## Construire et publier l'image Docker

Deux options : automatiquement via GitHub Actions (recommandé), ou manuellement depuis votre PC.

### Option A — Automatique via GitHub Actions (recommandé)

Le projet inclut `.github/workflows/docker.yml`. À chaque `git push` sur `main` (ou sur un tag `vX.Y.Z`), GitHub construit l'image **multi-architecture** (PC `amd64`, Raspberry Pi `arm64` et `arm/v7`) et la publie sur **GitHub Container Registry (ghcr.io)**. Aucun secret à créer : le `GITHUB_TOKEN` intégré suffit.

Mise en route (une seule fois) :
1. Poussez le projet sur GitHub (voir section précédente). Le workflow se déclenche tout seul.
2. Onglet **Actions** du dépôt → vérifiez que le job « Build and publish Docker image » passe au vert.
3. La première fois, le paquet est créé en **privé**. Pour le rendre public : page du dépôt → **Packages** (colonne de droite) → cliquez sur `wago2ha` → **Package settings** → **Change visibility** → *Public*. (Vous pouvez aussi le laisser privé ; il faudra alors `docker login ghcr.io` pour le tirer.)

Votre image est alors disponible à :
```
ghcr.io/<votre-utilisateur>/wago2ha:latest
```
Pour l'utiliser, décommentez la ligne `image:` dans `docker-compose.yml` (et commentez `build: .`), puis :
```bash
docker compose pull
docker compose up -d
```

> Note : les noms d'images sur GHCR doivent être en **minuscules**. Si votre nom d'utilisateur contient des majuscules, utilisez-le en minuscules dans `docker-compose.yml`.

### Option B — Manuelle depuis Windows (PowerShell)

Nécessite **Docker Desktop**. Pour publier sur GHCR, créez d'abord un *Personal Access Token* GitHub (classic) avec la portée `write:packages` (Settings → Developer settings → Personal access tokens).

```powershell
cd C:\Users\<vous>\Wago2HA

# Connexion (collez le token quand le mot de passe est demande)
docker login ghcr.io -u <votre-utilisateur>

# Construction (architecture de votre PC uniquement)
docker build -t ghcr.io/<votre-utilisateur>/wago2ha:latest .

# Publication
docker push ghcr.io/<votre-utilisateur>/wago2ha:latest
```

Pour une image **multi-architecture** en manuel (utile si HA tourne sur Raspberry Pi) :
```powershell
docker buildx create --use --name wago2ha-builder
docker buildx build `
  --platform linux/amd64,linux/arm64,linux/arm/v7 `
  -t ghcr.io/<votre-utilisateur>/wago2ha:latest `
  --push .
```

> Pour publier sur **Docker Hub** plutôt que GHCR : `docker login` (sans argument), puis remplacez `ghcr.io/<votre-utilisateur>` par `<votre-utilisateur-dockerhub>` dans les commandes ci-dessus.

---

## Limites et points à valider

Ce code implémente fidèlement le protocole reconstitué depuis Calaos, mais il **doit être validé sur votre matériel réel**. Points à vérifier en particulier :

- **Offsets Modbus** (`output_write_offset` / `output_read_offset`) : confirmés pour la famille 750-841/881 d'après le source Calaos, mais la cartographie exacte dépend de votre programme CODESYS. Vérifiez qu'écrire une sortie l'actionne bien physiquement.
- **Capteurs DALI (présence / luminosité)** : **non pris en charge.** Le firmware Calaos n'expose pas la lecture des multicapteurs DALI — la commande `WAGO_DALI_GET` ne renvoie que le statut/niveau d'un *ballast*, pas l'occupation ni le lux. Les types `dali_presence`/`dali_lux` sont ignorés s'ils figurent dans la config.
- **Port UDP** : 4646 par défaut (port Calaos historique). Ajustez `udp_listen_port` / `udp_plc_port` selon votre configuration.
- **Format de l'état booléen UDP** : la passerelle accepte `true/false` et `1/0`.

### Les entrées (interrupteurs) ne réagissent pas ?

C'est le symptôme d'un **routage d'IP serveur** non appliqué. L'automate ne pousse ses événements
`WAGO INT …` que vers l'IP mémorisée dans `Config.SERVER_IP` (mémoire RETAIN). Wago2HA la
(ré)annonce à chaque heartbeat via `WAGO_SET_SERVER_IP`. À vérifier si ça ne marche toujours pas :

- La passerelle doit tourner en **`network_mode: host`** (docker-compose). En mode *bridge*, l'IP
  auto-détectée serait l'IP interne du conteneur (172.x), injoignable par l'automate. Sinon, fixez
  explicitement `plc.gateway_ip` avec l'IP LAN de la machine hôte.
- Le port **4646/UDP** doit être ouvert en entrée sur l'hôte.
- Activez `LOG_LEVEL=DEBUG` : la ligne « 1ere entree recue de l'automate » confirme le routage.
- Vérifiez que le `var` de chaque entrée correspond bien au numéro poussé par l'automate
  (`WAGO INT <var> …`), qui est l'index relatif à la première entrée numérique.

N'hésitez pas à activer les logs détaillés (`LOG_LEVEL=DEBUG`) pour observer les trames pendant la mise au point.
```bash
docker compose run -e LOG_LEVEL=DEBUG wago2ha
```
