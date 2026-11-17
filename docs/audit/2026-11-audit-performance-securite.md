# Audit performance et sécurité — novembre 2026

Périmètre : API (`apps/api`), worker, migrations, infrastructure Docker/MQTT,
paquets `energy-core` et `ai-agents`, au commit `f2c07c3`.

Méthode :

* lecture du code, en particulier l'authentification, l'isolation entre tenants
  (organisations clientes), l'ingestion et les requêtes analytiques ;
* vérifications sur un vrai PostgreSQL 16 + TimescaleDB 2.17, Redis 7.4 et
  Mosquitto 2.0 (les mêmes images que `docker-compose.yml`) ;
* banc d'essai (benchmark) reproductible : `scripts/bench/seed.py` puis
  `scripts/bench/bench.py`. Il crée 3 organisations de 20 onduleurs, avec
  6 métriques toutes les 5 minutes pendant 30 jours, soit **3,1 M lignes**
  de télémétrie ;
* la suite de tests : 192 tests avant l'audit, **216 après**, tous verts, y
  compris le test MQTT de bout en bout.

## 1. Synthèse

| ID | Sévérité | Constat | Statut |
|----|----------|---------|--------|
| S-01 | Élevée | Le rôle applicatif peut activer lui-même le contournement RLS (`app.rls_bypass`) | **À planifier** (§4.1) |
| S-02 | Élevée | Derrière un proxy, tous les clients partagent l'IP du proxy, donc le même budget de limitation | Corrigé |
| S-03 | Moyenne | Pas de verrou par compte : force brute distribuée sur le mot de passe et le code MFA | Corrigé |
| S-04 | Moyenne | Un code TOTP pouvait être rejoué pendant 90 s | Corrigé |
| S-05 | Moyenne | Aucune limite de taille de corps de requête (déni de service mémoire) | Corrigé |
| S-06 | Moyenne | Coût LLM non borné : pas de limite par utilisateur, historique complet renvoyé au modèle | Corrigé |
| S-07 | Moyenne | Pas de `statement_timeout` : une requête lourde monopolise une connexion | Corrigé |
| S-08 | Faible | Une configuration de production dangereuse démarrait sans erreur | Corrigé |
| S-09 | Faible | En-tête HSTS absent | Corrigé |
| S-10 | Faible | Compteur de limitation `INCR` puis `EXPIRE` non atomique | Corrigé |
| S-11 | Faible | Aucun journal d'accès (`--no-access-log`) ni identifiant de corrélation | Corrigé |
| S-12 | Info | Jeton de session renvoyé dans le JSON de login en plus du cookie HttpOnly | À décider |
| S-13 | Info | Énumération de comptes via `/register` et via le temps de réponse de `/password/forgot` | À décider |
| S-14 | Info | Compte MQTT `gateway` partagé par toutes les passerelles ; Redis sans mot de passe | À planifier (Phase 3) |
| P-01 | Élevée | `ai/insights` : 15 s, car l'intégrale sur 30 jours était recalculée 3 fois | Corrigé : 6,2 s (11 ms en cache) |
| P-02 | Élevée | `energy/overview` : 2,3 s par appel, alors que les tableaux de bord l'interrogent en boucle | Corrigé : 1,7 s (7 ms en cache) |
| P-03 | Moyenne | Prédicats RLS évalués **ligne par ligne** (environ 20 % du temps de scan) | Corrigé (migration 0005) |
| P-04 | Moyenne | Envoi des notifications avec une transaction et des verrous tenus pendant l'appel SMTP | Corrigé |
| P-05 | Faible | Ingestion : un `UPDATE` par équipement et par lot | Corrigé : un seul `UPDATE` |
| P-06 | Faible | Authentification : 2 requêtes SQL par appel | Corrigé : 1 requête |
| P-07 | Faible | Middleware `BaseHTTPMiddleware` (une tâche et un flux en plus par requête) | Corrigé : ASGI pur |
| P-08 | Faible | Pool SQL figé aux valeurs par défaut (5 + 10) | Corrigé : paramétrable |
| P-09 | Moyenne | L'intégrale journalière reste coûteuse (≈ 1,3 s pour 30 j × 20 équipements) | À planifier (§4.2) |

Points solides relevés, à conserver : Argon2id ; jetons opaques dont seul le
hachage est stocké ; contrôle CSRF par en-tête ; double isolation (filtre
explicite + RLS) ; réponse 404 plutôt que 403 sur les ressources d'un autre
tenant ; secrets en `SecretStr` sans valeur par défaut ; readiness qui échoue
en mode fermé (fail closed) ; télémétrie en ajout seul (append-only) ; outils
LLM en lecture seule, liés à l'organisation de l'appelant. **Aucune injection
SQL n'a été trouvée** : toutes les requêtes `text()` sont paramétrées, et les
f-strings n'interpolent que des constantes internes.

## 2. Sécurité : détail

### S-01 — Contournement RLS par le rôle applicatif (à planifier)

Les politiques RLS lisent deux variables de session, `app.rls_bypass` et
`app.current_org_id`. Or **n'importe quel rôle** peut appeler `set_config`.
Reproduit sur la base de test, connecté en `solarmind_app` :

```sql
BEGIN;
SELECT set_config('app.current_org_id', gen_random_uuid()::text, true);
SELECT count(*) FROM organizations;          -- 0
SELECT set_config('app.rls_bypass', 'on', true);
SELECT count(*) FROM organizations;          -- 1 : l'organisation d'un autre tenant
```

Conséquence : la RLS protège contre un **oubli de filtre** dans le code (son
rôle principal, et elle le fait bien), mais **pas** contre une injection SQL
ou une exécution de SQL arbitraire. Une seule faille de ce type donnerait
accès à toutes les organisations. L'ADR-002 a été corrigée pour le dire.

Correctif proposé : §4.1. Il n'a pas été appliqué dans cette passe, car il
ajoute un secret à gérer en exploitation et touche chaque requête.

### S-02 — IP client derrière un proxy (corrigé)

`client_ip()` renvoyait `request.client.host`. Derrière Nginx ou un load
balancer, cette valeur est celle du proxy : un seul client pouvait épuiser le
budget de `/auth/login` pour tout le monde (déni de service), et la limite par
IP ne freinait plus aucun attaquant.

Correctif : `SOLARMIND_TRUSTED_PROXIES` (liste de plages CIDR, au format JSON).
`X-Forwarded-For` n'est lu que si la connexion vient d'un proxy de cette
liste. La chaîne est lue de droite à gauche : la première adresse non fiable
est le client, et les entrées ajoutées à gauche par le client sont ignorées
(`middleware.resolve_client_ip`, tests dans `tests/unit/test_edge.py`).

### S-03 / S-04 — Force brute et rejeu MFA (corrigé)

* Verrou **par compte** (`services/throttle.AccountGuard`) : après
  `login_max_failures` (10) mots de passe faux, ou `mfa_max_failures` (5)
  codes faux, en 15 minutes, le compte refuse toute tentative avec
  `429 account_locked`, même avec le bon mot de passe. Le compteur est remis
  à zéro après une connexion réussie.
* Anti-rejeu TOTP : un code accepté est réservé dans Redis
  (`SET NX EX 120`). Le même code est ensuite refusé.
* Redis indisponible : l'authentification est refusée (fail closed), comme le
  limiteur existant.

Limite assumée : connaissant une adresse e-mail, un attaquant peut bloquer ce
compte pendant 15 minutes. C'est le compromis recommandé par l'OWASP ASVS
(2.2.1). Une alternative plus douce consiste à ajouter un délai progressif.

### S-05 — Taille des requêtes (corrigé)

Le contrôle `max_batch_records` avait lieu *après* que Pydantic eut analysé
tout le JSON : un corps de 500 Mo était entièrement chargé en mémoire.
`EdgeMiddleware` refuse maintenant (`413 payload_too_large`) :

* dès l'en-tête `Content-Length`, avant de lire le corps ;
* en comptant les octets reçus quand le corps est envoyé par morceaux (chunked).

Limites : 1 Mio pour l'API, 8 Mio pour `/ingestion/`. Elles sont
paramétrables.

### S-06 — Assistant IA (corrigé)

* `rate_limit_ai_per_minute` (10) **par utilisateur**, en fail closed :
  chaque question peut déclencher plusieurs appels LLM payants.
* Seuls les `ai_history_max_messages` (20) derniers messages sont renvoyés au
  modèle, ce qui borne le coût et la latence des longues conversations.

### S-07 — Délai maximal par requête SQL (corrigé)

`db_statement_timeout_ms` (15 s) est appliqué à chaque connexion de l'API,
dans les paramètres du serveur asyncpg, donc sans aller-retour
supplémentaire. Le worker n'a pas de délai, car ses tâches parcourent tous
les tenants. Un test vérifie qu'un `pg_sleep(1)` est annulé au bout de
100 ms.

### S-08 à S-11 (corrigés)

* En `staging` et `production`, le démarrage échoue si `SECRET_KEY` fait
  moins de 32 caractères, si `COOKIE_SECURE` vaut false, ou si
  `WEB_BASE_URL` n'est pas en https (les liens d'invitation et de
  réinitialisation transportent des jetons).
* `Strict-Transport-Security` est envoyé quand `cookie_secure` est actif.
* Le compteur de limitation utilise `MULTI/INCR/EXPIRE NX` : une clé ne peut
  plus rester sans durée de vie.
* Chaque réponse porte `X-Request-ID` (repris du client s'il est sûr, généré
  sinon) et `Server-Timing`. Un journal d'accès JSON est écrit (méthode,
  chemin **sans** query string, statut, durée, IP, request_id). Toutes les
  lignes de log émises pendant une requête portent son `request_id`.

### S-12 à S-14 (à décider ou à planifier)

* **S-12** : `POST /auth/login` renvoie le jeton dans le JSON **et** dans le
  cookie HttpOnly. C'est utile pour les clients API, mais si le futur front
  Next.js le stocke en `localStorage`, une faille XSS suffira à le voler.
  Recommandation : le front utilise uniquement le cookie, et un paramètre
  pourra masquer le jeton pour les clients navigateur.
* **S-13** : `/register` répond `409 email_taken`, et `/password/forgot`
  envoie l'e-mail de manière synchrone seulement si le compte existe (écart de
  temps mesurable). Correctif : écrire l'e-mail dans l'outbox de notifications,
  pour qu'il parte en asynchrone dans tous les cas.
* **S-14** : déjà noté dans « Known gaps ». La clé API vérifiée pour chaque
  message limite l'impact du mot de passe MQTT partagé. Redis n'est exposé que
  sur le réseau Docker et en `127.0.0.1`, mais il faut lui ajouter
  `requirepass` (ou une ACL) avant tout déploiement multi-hôtes.

## 3. Performance : mesures

Médiane de 15 appels en processus (sans réseau), sur 3,1 M lignes :

| Endpoint | Avant | Après (cache vide) | Après (cache chaud) |
|----------|------:|-------------------:|--------------------:|
| `GET /auth/me` | 10,7 ms | 11,5 ms | — |
| `GET /telemetry/latest` | 93,8 ms | 68,1 ms | — |
| `GET /telemetry/series` 24 h (brut) | 28,7 ms | 32,6 ms | — |
| `GET /telemetry/series` 30 j (horaire) | 58,5 ms | 41,4 ms | — |
| `GET /energy/overview` | 2 325 ms | 1 742 ms | **7 ms** |
| `GET /ai/insights/sites/{id}` | 14 950 ms | 6 228 ms | **11 ms** |

Les écarts de ± 3 ms sur `/auth/me` et sur la série 24 h relèvent du bruit de
mesure : un autre passage a donné 9,3 ms et 24,0 ms. Le gain de P-06 (un
aller-retour SQL en moins) ne se voit pas en local. Il vaut environ un RTT
réseau par requête en production.

### P-01 — `ai/insights` (corrigé)

Le profilage par agent montrait que `daily_integrals` sur 30 jours (2,6 s)
était appelée 3 fois : anomalies, risque de maintenance et recommandations.
Chaque agent lisait aussi l'horloge séparément, donc les fenêtres ne
coïncidaient jamais.

Correctif : `ai.analysis_scope()` fige l'horloge pour une passe d'analyse, et
`energy.memoized()` mémoïse les intégrales (variables de contexte, limitées à
la requête). Les 4 comptages d'alertes sont regroupés en une requête
(`count() FILTER`). Résultat : de 15 s à 6,2 s.

### P-02 — Cache de lecture (nouveau)

`services/cache.py` met en cache, dans Redis, `energy/overview` (TTL 30 s) et
`ai/insights` (TTL 300 s). Règles de sûreté :

* la clé commence par l'`organization_id` ;
* les permissions sont vérifiées **avant** la lecture du cache ;
* les options du plan font partie de la clé ;
* si Redis est en panne, la valeur est calculée sans cache ;
* les écritures sur les sites et équipements invalident le cache de
  l'organisation en tâche d'arrière-plan, donc **après** le commit ;
* l'en-tête `X-Cache: HIT|MISS` indique la provenance.

Limites : les valeurs peuvent dater de 30 s au plus (le TTL). L'ingestion
n'invalide pas le cache : sinon une organisation active n'aurait jamais de
cache valide. L'invalidation fait un `SCAN` des clés de l'organisation : si
Redis contient des millions de clés, mieux vaut passer à un compteur de
version par organisation. Il n'y a pas non plus de protection contre la ruée
(stampede) : plusieurs absences simultanées calculent la valeur en parallèle.

### P-03 — RLS évaluée une fois par requête (corrigé, migration 0005)

`app_current_org()` est une fonction SQL « inlinable » : PostgreSQL la
remplace par `current_setting(...)` et l'évalue **pour chaque ligne lue**.
L'envelopper dans `(SELECT …)` en fait un `InitPlan`, calculé une seule fois
par requête. Sur l'intégrale de 16 jours (370 000 lignes) : 1 634 ms avec la
RLS d'origine, 1 308 ms avec la nouvelle, et 1 288 ms sans RLS du tout. Le
surcoût de la RLS devient négligeable, et la sémantique ne change pas (les
tests d'isolation passent).

### P-04 à P-08 (corrigés)

* Notifications : le worker réserve un lot (bail de 5 min) dans une
  transaction courte, envoie **hors transaction**, puis enregistre les
  résultats dans une autre transaction courte. Un fournisseur SMTP lent ne
  bloque plus une connexion et ses verrous. La garantie reste « au moins une
  fois » : après un crash, le message peut partir en double, mais jamais
  être perdu.
* Ingestion : `UPDATE devices … FROM (VALUES …)` en une seule instruction.
* Pool SQL : `db_pool_size`, `db_max_overflow`, `db_pool_recycle_s`.

## 4. Recommandations à planifier

### 4.1 Contexte tenant non falsifiable (S-01)

Option A, recommandée : un **contexte signé**. L'application calcule
`HMAC(clé, org_id || txid_current())` et le transmet avec l'organisation.
La politique appelle une fonction `SECURITY DEFINER` qui vérifie la
signature, avec une clé stockée dans une table que `solarmind_app` ne peut
pas lire. Une injection SQL ne connaît pas la clé, et la signature liée au
`txid` ne peut pas être rejouée dans une autre transaction. Pour garder les
performances, il faut envelopper l'appel dans `(SELECT …)`, comme en P-03.

```sql
CREATE FUNCTION app_current_org() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT CASE WHEN current_setting('app.org_sig', true) =
      encode(hmac(current_setting('app.current_org_id', true) || txid_current(),
                  (SELECT key FROM app_rls_key), 'sha256'), 'hex')
    THEN NULLIF(current_setting('app.current_org_id', true), '')::uuid END
$$;
```

Le même principe s'applique à `app.rls_bypass`. C'est une esquisse **non
testée** (les types de `hmac()` de pgcrypto sont à ajuster) : elle sert de
point de départ, pas de code prêt à déployer.

Option B : un second rôle de connexion `solarmind_system`, avec ses propres
identifiants, réservé aux transactions système. Les politiques testent alors
`current_user`. C'est plus simple, mais cela ne protège que le contournement,
pas le changement d'organisation.

Coût, dans les deux cas : un secret de plus à gérer et à faire tourner, et
une migration qui touche toutes les politiques. À traiter avant d'ouvrir la
plateforme à des clients B2G.

### 4.2 Matérialiser l'énergie journalière (P-09)

L'intégrale (fonction fenêtre `lag()` sur chaque échantillon) reste la
requête la plus chère. Un agrégat continu TimescaleDB ne peut pas contenir de
fonction fenêtre. Proposition : une table `energy_daily (organization_id,
site_id, metric_name, day, total, covered_s)`, remplie par le worker pour les
**jours clos**. Elle serait invalidée quand l'ingestion reçoit des données
tardives pour un jour déjà calculé (une passerelle qui vide son tampon).
Seul le jour en cours serait encore calculé à la volée. `overview` et
`insights` passeraient alors sous les 100 ms, même sans cache.

### 4.3 Divers

* Lancer uvicorn avec plusieurs workers (`--workers N`), ou derrière gunicorn.
  Le Dockerfile n'en lance qu'un.
* Dernières valeurs (`/telemetry/latest`, 68 ms) : une table « dernière
  valeur par (équipement, métrique) » mise à jour à l'ingestion, ou une
  requête `LATERAL` sur la clé primaire.
* Mettre à jour le README : le tableau « What works today » date de la
  Phase 1.

## 5. Nouvelles variables d'environnement

| Variable | Défaut | Rôle |
|----------|--------|------|
| `SOLARMIND_TRUSTED_PROXIES` | `[]` | Plages CIDR des proxies de confiance (JSON) |
| `SOLARMIND_MAX_REQUEST_BODY_BYTES` | 1048576 | Taille maximale d'un corps de requête API |
| `SOLARMIND_MAX_INGEST_BODY_BYTES` | 8388608 | Idem pour `/ingestion/` |
| `SOLARMIND_DB_POOL_SIZE` / `_MAX_OVERFLOW` | 10 / 10 | Taille du pool SQL |
| `SOLARMIND_DB_STATEMENT_TIMEOUT_MS` | 15000 | Durée maximale d'une requête SQL de l'API |
| `SOLARMIND_RATE_LIMIT_AI_PER_MINUTE` | 10 | Questions à l'assistant par utilisateur et par minute |
| `SOLARMIND_LOGIN_MAX_FAILURES` / `_MFA_MAX_FAILURES` | 10 / 5 | Échecs avant verrouillage du compte |
| `SOLARMIND_ACCOUNT_LOCK_WINDOW_S` | 900 | Fenêtre et durée du verrou |
| `SOLARMIND_CACHE_TTL_OVERVIEW_S` / `_INSIGHTS_S` | 30 / 300 | Durée de vie du cache (0 = désactivé) |
| `SOLARMIND_AI_HISTORY_MAX_MESSAGES` | 20 | Historique renvoyé au LLM |
