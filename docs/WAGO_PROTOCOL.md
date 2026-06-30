# Protocole Wago ↔ Calaos — référence vérifiée

> Source : programme CODESYS `calaos_wago/Wago_2.3/wago_881.pro` (POU `UDPServer`,
> `SendInput`, `PLC_PRG`, et la liste des variables globales). Ces informations sont
> extraites du **programme automate lui-même**, pas seulement du serveur `calaos_base`.
> Elles font autorité sur le format réel des trames.

## 1. Vue d'ensemble des canaux

| Canal | Sens | Rôle |
|---|---|---|
| **UDP / 4646** | Automate → serveur | Push des changements d'entrées TOR (`WAGO INT …`) |
| **UDP / 4646** | Serveur → automate | Heartbeat, config, commandes DALI/DMX, set output, etc. |
| **UDP / 4646** | Automate → serveur | Réponses aux requêtes `WAGO_GET_*` / `WAGO_DALI_GET` |
| **Modbus TCP / 502** | Serveur ↔ automate | Lecture entrées physiques, écriture image de sorties `netOutStandard`, lecture registres analogiques |

L'automate répond toujours **au dernier IP/port source** qui lui a parlé (l'`EthernetServer_FB`
mémorise `client_SRC_IP`/`client_SRC_PORT`). Son IP serveur « par défaut » (pour les push
d'entrées) est `Config.SERVER_IP`, en mémoire RETAIN, modifiable par `WAGO_SET_SERVER_IP`.

## 2. Le mécanisme HEARTBEAT (= suspension de la logique automate)

- L'automate maintient un `HEARTBEAT_TIMER` de **30 s**.
- Chaque trame `WAGO_HEARTBEAT` reçue le rearme.
- `HEARTBEAT = TRUE` tant que le timer n'a pas expiré.

Effet dans `PLC_PRG` :

| `HEARTBEAT` | `ManageOutput` (logique locale) | Source des sorties physiques |
|---|---|---|
| `TRUE` (serveur vivant) | **non exécuté** | `netOutStandard` (image réseau, écrite par Modbus) |
| `FALSE` (timeout 30 s) | exécuté | `OutArrState` (télérupteurs/volets calculés par l'automate) |

**Pour Wago2HA :** envoyer `WAGO_HEARTBEAT` toutes les ~10 s suffit pour que HA devienne le seul
cerveau, sans toucher au programme CODESYS. Sans heartbeat, les écritures de sorties sont ignorées.

## 3. Commandes entrantes (serveur → automate), UDP/4646, ASCII

Les paramètres sont séparés par des espaces. Le parsing automate utilise `Strncmp` (préfixe) +
`GET_PARAM_DINT(cmd, pos)` (n-ième entier après le préfixe, `pos` commençant à 1).

| Commande | Format | Effet |
|---|---|---|
| Heartbeat | `WAGO_HEARTBEAT` | Rearme le timer, `HEARTBEAT=TRUE`, LED « mode PC » |
| Set output | `WAGO_SET_OUTPUT <idx> <0\|1>` | Force une sortie TOR (offset `start_addr_out`). Effectif surtout hors heartbeat ; en heartbeat, `netOutStandard` (Modbus) prime |
| Set type sortie | `WAGO_SET_OUTTYPE <idx> <type>` | Définit le comportement *standalone* d'une sortie (voir §6) |
| Set adresses sortie | `WAGO_SET_OUTADDR <idx> <addr1> <addr2> <sameAs>` | Mappe la sortie : addr1=relais principal (MONTÉE volet), addr2=relais 2 (DESCENTE) ou adresse DALI, sameAs=alias d'une autre entrée |
| Get type sortie | `WAGO_GET_OUTTYPE <idx>` | Réponse `WAGO_OUTTYPE <idx> <type>` |
| Get adresses sortie | `WAGO_GET_OUTADDR <idx>` | Réponse `WAGO_OUTADDR <idx> <addr1> <addr2> <sameAs>` |
| Infos globales | `WAGO_GET_INFO` | Réponse `WAGO_INFO <nbMod> <nbModIn> <nbModOut> <nbInDig> <nbOutDig> <nbAnaIn> <nbAnaOut>` |
| Infos module | `WAGO_GET_INFO_MODULE <n>` | Réponse `WAGO_MODULE <n> <type> <pos> <sizePAE> <sizePAA>` |
| Version | `WAGO_GET_VERSION` (préfixe) | Réponse `…<H>.<L> 750-841` |
| Set IP serveur | `WAGO_SET_SERVER_IP a.b.c.d` | Fixe l'IP vers laquelle l'automate pousse les entrées |
| Set IP DMX | `WAGO_SET_DMX_IP a.b.c.d` | IP de l'interface DMX StageProfi |
| **DALI set** | `WAGO_DALI_SET <line> <grp> <addr> <pct> <fade>` | `grp`=1 groupe / 0 individuel. `pct` 0-100. Si `addr≥100` → canal DMX (`addr-100`), valeur `pct*255/100` |
| **DALI get** | `WAGO_DALI_GET <line> <shortAddr> <addr> …` | Lance une lecture ; réponse asynchrone `WAGO_DALI_GET <status 0\|1> <niveau>` |
| Volet get position | `WAGO_INFO_VOLET_GET <idx>` | Réponse `WAGO_INFO_VOLET <idx> <position>` (DWORD, stockage seul) |
| Volet set position | `WAGO_INFO_VOLET_SET <idx> <position>` | Écrit la position mémorisée (pas de recalcul automate) |
| DALI adressage | `WAGO_DALI_GET_ADDR <line>` | Découverte des adresses courtes (réponse via DaliSendAction) |
| DALI info device | `WAGO_DALI_GET_DEVICE_INFO <line> <addr>` | Lecture config d'un ballast |
| DALI groupes device | `WAGO_DALI_GET_DEVICE_GROUP <line> <addr>` | Lecture des groupes d'un ballast |
| DALI ajout groupe | `WAGO_DALI_DEVICE_ADD_GROUP <line> <addr> <grp>` | |
| DALI retrait groupe | `WAGO_DALI_DEVICE_DEL_GROUP <line> <addr> <grp>` | |
| DALI central on/off | `WAGO_DALI_CENTRAL <line> <0\|1>` | |
| DALI blink | `WAGO_DALI_BLINK <line> <addr> <grp> <time>` / `WAGO_DALI_BLINK_STOP <line>` | Identification visuelle |
| DALI config device | `WAGO_DALI_SET_DEVICE_CONFIG <line> <addr> <fadeRate> <fadeTime> <max> <min> <sysFail> <powerOn>` | |
| DALI nouvel adressage | `WAGO_DALI_ADDRESSING_NEW <line> <reset>` / `WAGO_DALI_ADDRESSING_ADD <line>` | |

## 4. Commande sortante (automate → serveur)

| Trame | Quand | Sens |
|---|---|---|
| `WAGO INT <idx> 1` | Front montant d'une entrée TOR | Bouton appuyé |
| `WAGO INT <idx> 0` | Front descendant | Bouton relâché |

C'est `SendInput` qui les émet, vers `Config.SERVER_IP:4646`. **Seules les entrées numériques
sont poussées en UDP.** Les valeurs analogiques (PT1000, etc.) se lisent en Modbus (registres
de maintien), pas par UDP.

La détection clic simple/double/triple et appui long est **calculée côté serveur** à partir de
ce flux on/off (fenêtres de 500 ms), pas par l'automate.

## 5. Modbus TCP (port 502)

| Opération | Fonction | Adresse |
|---|---|---|
| Lire une entrée TOR | FC01 read_coils | `var` |
| Écrire une sortie TOR | image `netOutStandard` (`%IB512`, fenêtre PFC à partir de `4096`) | dépend du mapping ; **n'agit que si `HEARTBEAT=TRUE`** |
| Relire l'état d'une sortie | FC01 read_coils | `var + 512` (0x200) |
| Lire un registre analogique | FC03 read_holding_registers | `var` |

`netOutStandard AT %IB512 : ARRAY[0..31] OF BYTE` (32 octets = 256 sorties TOR).
`netOutKNX AT %IB768`, `netInKNX AT %QB768` pour le KNX.

## 6. Types de sortie (`output_type`) — comportement standalone

Utilisés par `ManageOutput` quand `HEARTBEAT=FALSE`. Les fixer via `WAGO_SET_OUTTYPE` permet
de définir un **repli gracieux** : si la passerelle tombe, l'automate refait fonctionner la
maison en télérupteurs/volets locaux.

| Code | Type | Comportement |
|---|---|---|
| 0 | `NONE` | Rien (sortie pilotée uniquement par le réseau) |
| 1 | `TELERUPTEUR` | Bascule à chaque impulsion (FB `LIGHT`) |
| 2 | `DIRECT` | Recopie directe de l'entrée |
| 3 | `VOLET` | Volet 2 relais (addr1=MONTÉE, addr2=DESCENTE) |
| 4 | `VOLET_IMPULSE` | Volet impulsionnel (1 impulsion `impulse_time`=300 ms) |
| 5 | `TELERUPTEUR_DALI` | Télérupteur → DALI individuel (addr2 = adresse DALI ; addr2>99 → DMX) |
| 6 | `TELERUPTEUR_DALI_GROUP` | Télérupteur → groupe DALI |
| 7 / 8 | `*_KNX_OUTPUT` | KNX (uniquement 750-849) |

`output_addr[idx]` : `.ADDR1`, `.ADDR2` (BYTE), `.SameAs` (INT, -1 si aucun alias).

## 7. Les deux points « à valider sur matériel » — verdict

- **Mécanisme de suspension du programme :** ✅ **Résolu.** C'est le `WAGO_HEARTBEAT`. Aucune
  modification CODESYS nécessaire. (L'ancienne piste « bobine bRemoteMode » est abandonnée.)
- **Capteurs DALI présence / luminosité :** ⚠️ **Non exposés par ce firmware.** `WAGO_DALI_GET`
  lit le **statut/niveau de gradation d'un ballast** (`DALIDimmValue.xStatus` + `bDimmValue`),
  pas l'occupation ni le lux. Les multicapteurs DALI ne sont pas remontés par ce programme.
  Si tu en as besoin, il faudrait étendre le programme automate (lib `DALI_647_SensorType*`
  présente dans `Additionnal/`), ce qui sort du périmètre passerelle.

### À vérifier sur le niveau DALI
`WAGO_DALI_SET` envoie le niveau en **pourcent (0-100)** comme `bDimmLevel`. La réponse
`WAGO_DALI_GET` renvoie `DALIDimmValue.bDimmValue` (niveau DALI brut) sur la voie DALI, mais
un **pourcent** sur la voie DMX. Mappe donc la luminosité HA (0-255) → 0-100 à l'écriture, et
contrôle l'échelle de lecture sur ton matériel.
