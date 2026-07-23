# GAP REPORT — Cutover Plans SAP vs. Exodia (SAP Migration Toolkit)

**Âmbito:** 4 métodos de system copy × 4 fases do ciclo de vida (Preparation, Ramp-Down, Downtime/Execution, Post-Activities).
**Objetivo:** identificar que *checks* (read-only) e *actions* (mudam estado) faltam implementar face ao que o toolkit já tem.

**Legenda:**
- `[JÁ]` = já implementado no toolkit · `[FALTA]` = por implementar.
- **check** = read-only (evidência) · **action** = muda estado (fluxo guarded).
- **BLOCK** = bloqueante (o cutover não deve avançar se falhar) · **n-block** = não bloqueante.
- As fases cross-cutting ABAP (`abap.rampdown.*`, `abap.post.*`), PI/PO (`pipo.*`) e SolMan já existem e servem TODOS os métodos via o eixo Fase; não se repetem por método salvo quando há um gap específico do método.

---

## 1. Backup & Restore (HANA, homogéneo) — SWPM p/ rename ABAP

Estado: 14 checks preparation + 2 actions downtime (`restore-database`, `swpm.system-copy`). **Zero** ramp-down e **zero** post específicos do método.

### Preparation
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Backup de dados presente e recuperável | [JÁ] `data-backup-present` | check | BLOCK |
| Log backups contínuos / log mode normal | [JÁ] `log-backups-continuous`, `log-mode-normal` | check | BLOCK |
| Integridade do catálogo de backup | [JÁ] `catalog-integrity` | check | BLOCK |
| Backint / chaves de encriptação / userstore | [JÁ] `backint-config`, `encryption-keys`, `userstore-key` | check | BLOCK |
| Espaço data/log no target, portas, permissões sidadm, versão | [JÁ] `target-data-space`, `target-log-space`, `ports-available`, `sidadm-permissions`, `version-compatibility` | check | BLOCK |
| Minichecks / SICK-equivalente / sanidade SID | [JÁ] `minichecks`, `sid-instance-sanity` | check | BLOCK |
| **Definir e validar a estratégia de recovery** (point-in-time vs. most-recent, catálogo correto a usar, `RECOVER DATABASE UNTIL`) | **[FALTA]** `backup-restore.hana.recovery-strategy` | check | BLOCK |
| **Compatibilidade de plataforma/endianness source↔target** (mesma versão HANA major, patch >=, SP do SO) | **[FALTA]** `backup-restore.hana.platform-compatibility` | check | BLOCK |
| **Presença/validade do parameter file de recovery e do path do backup no target** (backup acessível a partir do target) | **[FALTA]** `backup-restore.hana.backup-reachable-from-target` | check | BLOCK |
| **License key SAP disponível para o target** (evita HANA lock 28 dias / SAP license) | **[FALTA]** `backup-restore.hana.target-license` | check | n-block |
| **Export de parâmetros/kernel/SPS alinhados** (kernel target >= source, SPAM/SAINT) | **[FALTA]** `backup-restore.abap.kernel-sps-alignment` | check | n-block |

### Ramp-Down
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Lock users, suspend jobs (BTCTRNS1), stop app servers, operation modes (SM63), inform customer | [JÁ] via `abap.rampdown.*` (cross-cutting) | action | BLOCK |
| **Backup final consistente / último log backup antes de parar** (garantir RPO no source homogéneo) | **[FALTA]** `backup-restore.hana.final-log-backup` | action | BLOCK |
| **Registar o ponto de corte** (timestamp/último log position para o recovery UNTIL) | **[FALTA]** `backup-restore.hana.capture-recovery-point` | check | n-block |

### Downtime / Execution
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Recovery da base HANA no target | [JÁ] `restore-database` (driver hana) | action | BLOCK |
| SWPM system copy (target system / rename) | [JÁ] `swpm.system-copy` | action | BLOCK |
| **Monitorização do recovery** (progresso `M_BACKUP_CATALOG`/recovery log, ETA) | **[FALTA]** `backup-restore.hana.recovery-monitor` | check | n-block |
| **Arranque pós-recovery e verificação de tenant/DB online** (antes do SWPM ABAP correr) | **[FALTA]** `backup-restore.hana.post-recovery-online` | check | BLOCK |

### Post-Activities
Nada implementado neste método (o `swpm.system-copy` faz o rename, mas os passos SAP-específicos de post-copy ABAP não estão cobertos por este método — dependem do cross-cutting `abap.post.*`, que existe, mas faltam os passos de conversão de sistema).
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Start app servers, resume jobs (BTCTRNS2), unlock users, validate online (SM51) | [JÁ] via `abap.post.*` | action | BLOCK |
| **BDLS — conversão de logical system names** (renomear cliente lógico source→target) | **[FALTA]** `backup-restore.abap.bdls-logical-system` | action | BLOCK |
| **SGEN — regeneração de loads ABAP** (evitar dumps/latência no arranque) | **[FALTA]** `backup-restore.abap.sgen-load-generation` | action | n-block |
| **SPAU/SPDD + SICK/SM28 pós-copy** (consistência de instalação) | **[FALTA]** `backup-restore.abap.installation-consistency` | check | n-block |
| **STMS — reconfigurar Transport Management** (remover source da rota, TMS domain) | **[FALTA]** `backup-restore.abap.stms-reconfigure` | action | BLOCK |
| **Limpar dados de runtime/específicos do source** (spool SP01, jobs SM37 órfãos, RFC SM59 apontando ao source, batch input SM35) | **[FALTA]** `backup-restore.abap.purge-source-runtime` | action | n-block |
| **STRUST/SSFS — reconstruir SSO/PSE se rename** | **[FALTA]** `backup-restore.abap.strust-rebuild` | action | n-block |
| **Consistência de dados source-vs-target** (row/table counts de tabelas-chave pós-recovery) | **[FALTA]** `backup-restore.hana.data-consistency` | check | n-block |

---

## 2. Export & Import (SWPM, heterogéneo/independente de DB) — R3load/JLoad

Estado: **apenas 5 checks preparation**. Zero actions, zero ramp-down, zero downtime, zero post. **É o método mais incompleto** — não executa nada.

### Preparation
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| DB client acessível, load tool para a stack, SWPM presente | [JÁ] `db-client-reachable`, `load-tool-for-stack`, `swpm-present` | check | BLOCK |
| Consistência do export, espaço no dir de export | [JÁ] `export-consistency`, `export-dir-space` | check | BLOCK |
| **Table splitting / package splitter preparado** (`R3ta`/`str_splitter`, tabelas grandes para paralelismo) | **[FALTA]** `export-import.r3load.table-splitting-plan` | check | n-block |
| **Consistência de DB antes do export** (DB02/DBACOCKPIT, sem tabelas inconsistentes) | **[FALTA]** `export-import.source.db-consistency` | check | BLOCK |
| **Migration key / DMIS / distribution monitor config** (chave de migração SAP para heterogéneo) | **[FALTA]** `export-import.migration-key` | check | BLOCK |
| **Unicode / code page compatibility** (source→target charset, para non-Unicode legacy) | **[FALTA]** `export-import.unicode-compatibility` | check | BLOCK |
| **Espaço/IO no target DB e nos filesystems de import** | **[FALTA]** `export-import.target.import-space` | check | BLOCK |

### Ramp-Down
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Lock users, suspend jobs, stop app servers, inform customer | [JÁ] via `abap.rampdown.*` | action | BLOCK |
| **Verificação de sistema quiescido antes do export** (0 updates SM13, 0 locks SM12, queues qRFC/tRFC drenadas SMQ1/2) | **[FALTA]** `export-import.source.quiesced-verify` | check | BLOCK |

### Downtime / Execution
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| **Correr o export R3load/JLoad no source** (orquestração sapinst export DB-independent) | **[FALTA]** `export-import.swpm.export` | action | BLOCK |
| **Monitorizar o export** (R3load logs, `export_monitor`, tempos por package, erros) | **[FALTA]** `export-import.r3load.export-monitor` | check | BLOCK |
| **Transferir o dump export source→target** (verificar integridade/checksum na chegada) | **[FALTA]** `export-import.transfer-export` | action | n-block |
| **Correr o import R3load/JLoad no target** (orquestração sapinst import) | **[FALTA]** `export-import.swpm.import` | action | BLOCK |
| **Monitorizar o import** (`import_monitor`, tabelas falhadas, índices, retry de packages) | **[FALTA]** `export-import.r3load.import-monitor` | check | BLOCK |
| **Verificar integridade pós-import** (todos os packages `+++`/OK, sem `E` no *_monitor log) | **[FALTA]** `export-import.import-integrity` | check | BLOCK |

### Post-Activities
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Start app servers, resume jobs, unlock users, validate online | [JÁ] via `abap.post.*` | action | BLOCK |
| **BDLS, SGEN, STMS, SPAU/SPDD, SICK, purge de runtime source** | **[FALTA]** partilhar/derivar do bloco `backup-restore.abap.*` (BDLS, SGEN, STMS, installation-consistency, purge-source-runtime) | action/check | BLOCK (BDLS/STMS) |
| **Recriar estatísticas do DB e índices** (BRCONNECT/DBACOCKPIT update stats pós-import) | **[FALTA]** `export-import.target.db-statistics` | action | n-block |
| **Consistência source-vs-target row counts** (validar que o import não perdeu registos) | **[FALTA]** `export-import.data-consistency` | check | BLOCK |

---

## 3. Tenant Copy (HANA MDC) — o mais maduro (29 ops)

Estado: 19 preparation (17 checks + 2 actions), 4 downtime actions, 6 post. Cobertura boa. Gaps são de refinamento e de ramp-down.

### Preparation
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Readiness source/target/combinado, portas, params replicação, userstore, versão, espaço, license, catálogo/tabela consistência, tenant target ausente, SSL collateral | [JÁ] `readiness*`, `source-*`, `target-*`, `ssl-collateral`, `version-match`, `cross-host-reachability`… | check | BLOCK |
| Configurar parâmetros HSR (SSL on/off), restart HANA | [JÁ] `configure-hsr-parameters`, `restart-hana` | action | BLOCK |
| **Verificar `system_replication` / global.ini prontos para o CREATE DATABASE AS REPLICA** (se via HSR-based tenant move) | **[FALTA]** `tenant-copy.hana.replica-prereqs` | check | n-block |

### Ramp-Down
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Lock users, suspend jobs, stop app servers (ABAP) | [JÁ] via `abap.rampdown.*` | action | BLOCK |
| Mock-isolate (dry-run): isolar RFCs, users, parar jobs | [JÁ] `mock-isolate-rfcs`, `mock-isolate-users`, `mock-stop-jobs` (fase downtime, mas são o ensaio do ramp-down) | action | n-block |
| **Verificar tenant source quiescido antes do copy** (sem transações abertas, checkpoint feito) | **[FALTA]** `tenant-copy.hana.source-quiesced` | check | BLOCK |

### Downtime / Execution
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Copiar o tenant (`hdbsql` CREATE DATABASE … AS REPLICA / copy) | [JÁ] `copy-tenant` | action | BLOCK |
| Mock isolation actions | [JÁ] `mock-isolate-*`, `mock-stop-jobs` | action | n-block |
| **Monitorizar a cópia/sync do tenant** (progresso replicação, MB shipped) | [JÁ, parcial] streaming no `copy-tenant` (live dashboard) | — | — |

### Post-Activities
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Consistência (catalog/table), secure-comm, reconnect-verify, delete ABAP dict data | [JÁ] `data-consistency`, `secure-communication`, `target-catalog-consistency`, `target-table-consistency`, `reconnect-verify`, `delete-abap-dict-data` | check/action | BLOCK |
| **BDLS no target ABAP após reconnect** (renomear logical system se o tenant serve outro SID) | **[FALTA]** `tenant-copy.abap.bdls-logical-system` | action | n-block |
| **Reset de userstore/secure store do ABAP para o novo tenant** (hdbuserstore SET no target) | **[FALTA]** `tenant-copy.hana.target-userstore-set` | action | n-block |

---

## 4. HSR (HANA System Replication)

Estado: **apenas 6 checks preparation**. Zero actions (não configura, não faz takeover), zero downtime, zero post. Segundo método mais incompleto.

### Preparation
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Backup de dados existe, hosts distintos, log mode normal, portas replicação, status replicação, versão | [JÁ] `data-backup-exists`, `distinct-hosts`, `log-mode-normal`, `replication-ports-reachable`, `replication-status`, `version-compatibility` | check | BLOCK |
| **Parâmetros HSR presentes/alinhados** (global.ini `[system_replication]`, `logshipping_timeout`, `operation_mode`) | **[FALTA]** `hsr.replication-parameters` | check | BLOCK |
| **Site names / systemPKI SSFS trocados entre hosts** (pré-req do `hdbnsutil -sr_register`) | **[FALTA]** `hsr.pki-ssfs-exchanged` | check | BLOCK |

### Ramp-Down (aplicável quando HSR é usado para *mover* um sistema)
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Lock users / stop app servers no source antes do takeover | [JÁ] via `abap.rampdown.*` | action | BLOCK |
| **Verificar replicação SYNC e em `ACTIVE` antes de quiescer** (RPO=0 garantido) | **[FALTA]** `hsr.sync-active-verify` | check | BLOCK |

### Downtime / Execution
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| **Ativar primary (`hdbnsutil -sr_enable`) no source** | **[FALTA]** `hsr.enable-primary` | action | BLOCK |
| **Registar secondary (`hdbnsutil -sr_register`) no target** | **[FALTA]** `hsr.register-secondary` | action | BLOCK |
| **Monitorizar a sincronização inicial** (`M_SERVICE_REPLICATION` shipped/full, chegar a ACTIVE/SYNC) | **[FALTA]** `hsr.sync-monitor` | check | BLOCK |
| **Takeover (`hdbnsutil -sr_takeover`) no target** | **[FALTA]** `hsr.takeover` | action | BLOCK |
| **Verificar target promovido a primary e DB online** | **[FALTA]** `hsr.post-takeover-online` | check | BLOCK |

### Post-Activities
| Passo do plano SAP | Estado | Nome sugerido | Tipo | BLOCK |
|---|---|---|---|---|
| Start app servers, resume jobs, unlock users, validate online | [JÁ] via `abap.post.*` | action | BLOCK |
| **Desregistar/limpar a relação HSR (`hdbnsutil -sr_unregister` / `-sr_disable`)** se o alvo era mover e não HA | **[FALTA]** `hsr.unregister-cleanup` | action | BLOCK |
| **Reconfigurar app ABAP para novo host/IP HANA** (default.pfl, hdbuserstore, connectivity) | **[FALTA]** `hsr.abap-reconnect` | action | BLOCK |
| **BDLS/SGEN/STMS se o sistema mudou de identidade** | **[FALTA]** derivar de `backup-restore.abap.*` | action/check | BLOCK (BDLS/STMS) |

---

## Observações transversais (aplicam-se a vários métodos)

- **PI/PO (AS Java)** já tem post-activities (`pipo.rebuild-secstore`, `register-sld`, `fix-rfc-jco`, `reconfigure-ume`, `postcopy-all`). Aplicáveis a qualquer método quando a stack é Java — não é gap.
- **Solution Manager** (`lmdb-reachable`, `managed-system-connectivity`, `no-stale-source-registration`, `pca-tasklist-available`, `sld-reachable`) já existe como preparation cross-cutting — não é gap.
- Os passos ABAP de post-copy (**BDLS, SGEN, STMS, SPAU/SPDD, purge de runtime source**) são o **maior gap partilhado**: nenhum método os implementa como actions/checks, embora `abap.post.*` cubra start/stop/unlock/resume. Devem ser um bloco `abap.post.*` reutilizável (não duplicar por método).

---

## TOP 10 — o que implementar primeiro (por impacto real no cutover)

Prioridade = "sem isto, um cutover real falha ou fica inconsistente".

| # | Método | Fase | Nome | Tipo | Porquê é #topo |
|---|---|---|---|---|---|
| 1 | Export & Import | downtime | `export-import.swpm.export` + `export-import.swpm.import` | action | O método não executa NADA hoje. Sem export/import não há system copy heterogéneo. Núcleo em falta. |
| 2 | HSR | downtime | `hsr.enable-primary` + `hsr.register-secondary` + `hsr.takeover` | action | HSR só faz checks; não configura nem faz takeover. Sem estas 3 actions o método não move nenhum sistema. |
| 3 | (cross) ABAP | post | `abap.post.bdls-logical-system` (BDLS) | action | Sem BDLS o sistema copiado mantém logical system names do source → IDocs/RFC/ALE partem. Bloqueante em TODOS os métodos. |
| 4 | (cross) ABAP | post | `abap.post.stms-reconfigure` (STMS) | action | Transport domain aponta ao source → risco de transportes cruzados entre PRD e cópia. Bloqueante. |
| 5 | Export & Import | downtime | `export-import.r3load.import-monitor` + `export-import.import-integrity` | check | Import R3load falha silenciosamente por package; sem monitor/integrity dá-se por "pronto" um DB incompleto. |
| 6 | Backup & Restore | preparation | `backup-restore.hana.recovery-strategy` | check | Escolher o catálogo/point-in-time errado = recovery falha ou perde dados. Decisão nº1 do restore. |
| 7 | HSR | preparation | `hsr.sync-active-verify` | check | Fazer takeover sem replicação ACTIVE/SYNC = perda de dados. Guarda-corpo do RPO=0. |
| 8 | Export & Import | preparation | `export-import.unicode-compatibility` + `export-import.migration-key` | check | Migração heterogénea sem migration key / com mismatch de code page aborta o SWPM a meio da downtime. |
| 9 | Backup & Restore | downtime | `backup-restore.hana.post-recovery-online` | check | Antes de correr o SWPM ABAP tem de se confirmar que o DB recuperou e está online — senão o SWPM falha tarde. |
| 10 | (cross) ABAP | post | `abap.post.sgen-load-generation` (SGEN) | action | Sem SGEN o sistema arranca com dumps/latência massiva; go-live percecionado como falhado. Alto impacto de perceção. |

**Nota de arquitetura:** os itens #3, #4, #10 (BDLS/STMS/SGEN) e o `purge-source-runtime` devem viver UMA vez em `abap.post.*` (cross-cutting) e ser reutilizados por Backup&Restore, Export&Import e HSR — não reimplementados por método. Tenant Copy precisa deles só quando o tenant serve um SID ABAP diferente.
