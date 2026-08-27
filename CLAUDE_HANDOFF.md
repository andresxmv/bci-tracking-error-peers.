# Handoff para Claude — BCI Tracking Error Peers

Fecha del handoff: **27-08-2026**  
Repositorio: `https://github.com/andresxmv/bci-tracking-error-peers`  
Rama: `main`  
HEAD de partida: `607ea93` (`Fix mixed CLP and PROM FX conversion`); la corrección
de configuración pendiente se publicará en un commit posterior.
Servicio Railway: `bci-tracking-error-final`  
URL pública: [bci-tracking-error-final-production.up.railway.app](https://bci-tracking-error-final-production.up.railway.app/)

Este documento resume el proyecto para continuar el diagnóstico con Claude. El último pedido del usuario es preparar documentación; **no se debe cambiar código ni desplegar otra versión como parte de este handoff**.

## 1. Resumen ejecutivo

La aplicación es un dashboard Flask para comparar fondos BCI con sus peer groups. La versión que debe considerarse autoritativa es `flask_app_v5.py`, iniciada por `app.py` y desplegada en Railway desde el HEAD `e49101f`. Incluye:

- Selección de fondo BCI.
- Selector completo de todos los peer RUN definidos en `fondos_config.json`, no sólo los marcados por defecto.
- Selector de fecha de corte.
- Recalculo de retornos, alpha, tracking error e information ratio para el escenario seleccionado.
- Information Ratio de 12 meses y Information Ratio YTD.
- Hero visual Three.js en el dashboard (`canvas#risk3d`).
- Flujo `/login` → `/actualizar` para descargar una cartola CMF con CAPTCHA.
- Persistencia de cartolas y retornos brutos para que una fecha ya cargada no vuelva a abrir un CAPTCHA.

La auditoría del corte **31-07-2026** ya confirma el YTD correcto de ambos fondos “Activa”:

| Fondo | RUN BCI | Evidencia local al 31-07-2026 | Comentario |
|---|---:|---:|---|
| CD Activa | 8640 | `0.11663718308130155` = **11,6637%** | Coincide con el valor que el usuario dice esperar (11,66–11,7%). |
| CP Activa | 9060 | `0.11004097139211821` = **11,0041%** | Es el valor de una auditoría anterior, con el P-group antiguo de 4 peers. |

No asumir que el problema es un duplicado sin reproducirlo con el fondo y los RUN correctos. La cartola local de julio se revisó con el parser del proyecto: **88.632 filas**, fechas 01-07 a 31-07 y sin duplicados por `fecha/RUN/serie`.

## 2. Criterio de aceptación para corregir el pendiente

Antes de editar:

1. Confirmar si “asset de Activa” significa **CD Activa (RUN 8640)** o **CP Activa (RUN 9060)**.
2. Comparar, para el escenario exacto que ve el usuario, el valor del dashboard con:
   - el cierre de cuota del 31-07;
   - el último nivel anterior al 01-01-2026;
   - los retornos diarios brutos por RUN;
   - el P-group realmente seleccionado;
   - el baseline validado en `panel_metrics_reference.json`.
3. Determinar si el desfase viene de la composición del baseline, de la selección de peers, de una fecha de cuota, de normalización RUN/serie o de persistencia runtime.
4. Agregar primero una prueba de regresión que reproduzca el caso.
5. Sólo después de demostrar la causa, aplicar una corrección mínima que conserve las fórmulas de TE, IR, alpha y retorno bruto.

El resultado esperado para CD Activa en 31-07 es aproximadamente **0,116637 (11,6637%)**, que la interfaz puede mostrar como **11,66%** o **11,7%** según el redondeo. No usar el número 11,88% como nueva referencia sin reconciliar los datos.

## 3. Historial de cambios relevantes

Todos estos commits están en `main`; el último contiene a los anteriores:

| Commit | Cambio |
|---|---|
| `c537dbca49b0d1e747aa9121dc465e1ec8c02e0f` | Hero financiero Three.js y presentación moderna del dashboard. |
| `b9615bf` | Texto del hero simplificado a **Peer Desempeño**. |
| `464f240` | Etiquetas de tracking error. |
| `7b60ce5` | Information Ratio YTD. |
| `6bfa535` | Controles de fecha de corte y escenario de peers. |
| `bcf12b9` | Selector de todos los peers históricos/configurados. |
| `c5a6cb8` | Reconstrucción de P-groups desde el Excel y caché de cartolas. |
| `e525d11` | Evita recomponer dos veces cierres solapados, especialmente 31-07. |
| `e49101f` | Alinea el YTD de un corte personalizado con el baseline calendario validado. |

El requisito original de Railway era desplegar `c537dbca...` o un commit posterior que lo contenga. `e49101f` cumple eso.

## 4. Arquitectura y archivos

| Archivo | Responsabilidad |
|---|---|
| `app.py` | Bootstrap. Imprime `APP_BOOTSTRAP_TO_FLASK_V5` y hace `os.execv` hacia `flask_app_v5.py`. Esto evita que un arranque legacy con Streamlit use otra aplicación. |
| `Dockerfile` | Python 3.13 slim, dependencias, Chromium de Playwright, puerto 8080 y `CMD` que arranca Flask v5. |
| `flask_app_v2.py` | Flask base, carga de datos comprimidos, configuración, formato, catálogo BCI, login, actualización CMF y rutas base. |
| `flask_app_v4.py` | Unión de niveles históricos con retornos brutos, catálogo de peers, escenarios personalizados, TE/IR/YTD y filas del peer table. |
| `flask_app_v5.py` | Capa autoritativa de runtime: corrección YTD, recomputación al guardar cartola, reemplazo de referencias usadas por el dashboard y health check. |
| `quota_update.py` | Parseo/validación de cartolas, retornos brutos, deduplicación y archivos runtime. |
| `cmf_automation.py` | Sesión CMF, proxy opcional, preparación del CAPTCHA y descarga de cartola para una fecha objetivo. |
| `templates/base.html` | Shell oscuro compartido, navegación, estilos base y Chart.js. |
| `templates/dashboard.html` | Hero Three.js, selector de fondo, fecha de corte, selector de peers, KPIs, gráfico TE y tabla peer. |
| `templates/login.html` | PIN de administración. |
| `templates/update.html` | Fecha de cartola, imagen CAPTCHA y submit de descarga. |
| `fondos_config.json` | Configuración vigente de fondos, RUN BCI, grupo y peers. Es la fuente del selector. |
| `panel_metrics_reference.json` | Baseline estático validado, principalmente al 21-08-2026. |
| `metrics_data.py` | Métricas estáticas comprimidas heredadas del panel original. |
| `series_data_1.py`, `_2.py`, `_3.py` | Niveles históricos comprimidos heredados del panel original. |
| `tests/test_quota_gaps.py` | Regresiones para saltos de cartola, solapes, duplicados, YTD custom y caché. |

### Arranque

`app.py` no ejecuta Streamlit: redirige el proceso al Flask v5. El `Dockerfile` también arranca directamente `flask_app_v5.py` con `PORT` (Railway usa 8080).

## 5. Configuración de P-groups

`fondos_config.json` refleja exactamente la columna `Run` del Excel entregado por
el usuario (`Diccionario Categorías.xlsx`): **25 fondos BCI**, **18 grupos** y
**219 RUN únicos**. Se conserva el BCI de cada categoría en el campo `bci` y se
guardan como peers los demás RUN de esa categoría. No hay exclusiones globales:

```python
EXCLUDED_RUNS = frozenset()
```

Los fondos BCI configurados son: CD Activa, CD Balanceada, CD Conservadora, CP Activa, CP Balanceada, CP Conservadora, Estratégico $ H 1 Año, Estratégico UF H 1 Año, Estratégico UF H 3 Años, Estratégico UF H 5 Años, Estratégico UF > 5 Años, Asia, Europa, Emergente Global, Estados Unidos, Global Titan, Acciones Chilenas, Top Picks, Acciones Globales y América Latina.

Configuración especialmente relevante:

```text
CD Activa  : BCI 8640; peers 9473, 9193, 9593, 9576, 8740, 8993, 9873, 8448, 9648
CP Activa  : BCI 9060; peers 8908, 8435, 8785, 8844, 10064
```

La configuración actual de CP Activa tiene **5 peers** porque así aparece en el Excel. El baseline antiguo de `panel_metrics_reference.json` dice “Peer group (4 fondos)” y contiene un benchmark histórico con 4 peers. Esa diferencia es importante: no mezclar automáticamente el baseline viejo con la nueva composición sin documentarlo y probarlo.

Top Picks contiene los 27 peers del Excel, incluidos `9362`, `8490`, `9537`,
`9685`, `10068`, `8043` y `10331`; Acciones Chilenas contiene sus 13 peers,
incluidos `9537`, `10068` y `10331`. Los RUN configurados sin cartola diaria
actual siguen visibles y se marcan `sin historia cargada`; al cargar su cartola
se incorporan al cálculo sin editar la configuración.

Los nombres mostrados en los desplegables provienen de `grupo` en `fondos_config.json`. Los peers sin niveles embebidos se siguen mostrando y aparecen con la marca `sin historia cargada`; si existe una cartola runtime, se les crea un nivel sintético 100 para poder calcular retornos relativos.

## 6. Flujo de la interfaz

### Dashboard

Ruta: `GET /`.

Parámetros relevantes:

- `fondo=<RUN>`: fondo BCI seleccionado.
- `peer_config=1`: activa un escenario explícito.
- `peer=<RUN>`: puede repetirse para cada peer seleccionado.
- `fecha_corte=YYYY-MM-DD`: fecha del escenario.

El formulario de configuración muestra todos los peers disponibles, permite buscar, seleccionar visibles, limpiar y aplicar. La fecha se acota al rango común de los RUN seleccionados; si el día no tiene cuota, el cálculo usa el último cierre anterior disponible.

El hero actual sólo tiene el texto **Peer Desempeño**, la fecha de corte y el canvas `#risk3d`. Three.js se importa desde jsDelivr y tiene fallback visual si WebGL/CDN falla. No eliminarlo.

### Login y actualización de cuota

Flujo: `/login` → PIN → `/actualizar`.

En `/actualizar`:

1. Se elige una fecha.
2. `has_quota_date()` revisa `quota_history.csv` y `latest_quota.csv`.
3. Si la fecha ya existe, se informa “fecha ya cargada” y **no se abre otro CAPTCHA**.
4. Si no existe, `CMFQuotaSession.prepare()` obtiene el CAPTCHA y conserva la sesión.
5. Al enviar el código, CMF devuelve la cartola; se parsea, valida cobertura y se persiste.
6. `flask_app_v5.persist_quota_and_recompute()` guarda y recomputa las referencias.

No abrir ni resolver un CAPTCHA para investigar el YTD si la fecha ya está en los archivos. Primero inspeccionar datos y deduplicación.

## 7. Datos runtime y cartolas

Por defecto, Railway escribe en `/app/runtime_data` (variable `DATA_DIR`):

```text
/app/runtime_data/latest_quota.csv
/app/runtime_data/quota_history.csv
/app/runtime_data/gross_returns_history.csv
/app/runtime_data/quota_status.json
```

Estos archivos no están en Git. Si el servicio no tiene un volumen persistente, se pierden al recrear el contenedor y la app vuelve al baseline embebido. Esto explica el comportamiento observado tras el último deploy: el build fue exitoso, pero el runtime no tenía una cartola posterior al baseline.

`parse_quota_file()` normaliza fechas/RUN, descarta cuotas inválidas y deduplica por `fecha`, `run`, `serie`. `persist_quota()` vuelve a deduplicar el histórico y regenera `gross_returns_history.csv`. `load_gross_returns()` deduplica por `fecha`, `run` para proteger archivos generados por versiones anteriores.

La sesión CMF usa:

- `BASELINE_DATE = 2026-08-21`.
- Para un objetivo igual o posterior al baseline, descarga desde `max(2026-08-21, objetivo - 30 días)`.
- Para un objetivo anterior al baseline, descarga desde `max(01-01 del año, objetivo - 7 días)`.

## 8. Metodología que no se debe cambiar

Estas reglas son invariantes del proyecto y deben conservarse salvo una decisión explícita del usuario:

### Retorno bruto

Para cada serie se usa la cuota ajustada por reparto/ajuste y se agregan remuneraciones/gastos sobre el patrimonio previo:

```text
retorno_serie = (valor_cuota * factor_reparto * factor_ajuste)
                 / valor_cuota_anterior - 1
                 + gastos_totales / patrimonio_anterior
```

El retorno del fondo se agrega usando pesos de patrimonio del día anterior. Para gaps entre observaciones mayores a **7 días**, el retorno se enmascara; nunca se interpreta un salto de varias semanas como un único retorno diario.

### Tracking Error EWMA

- Retorno activo: fondo menos promedio simple de los peers.
- `lambda = 0,94`.
- Anualización: `sqrt(52)` cuando corresponde.
- No reemplazar EWMA por desviación estándar simple ni cambiar la ventana sin autorización.

### Information Ratio

- `Information Ratio 12M`: retornos activos semanales, ventana de 52 semanas, media activa anualizada (`media * 52`) dividida por TE EWMA anualizado.
- `Information Ratio YTD`: retornos activos semanales desde el 01-01 del año calendario hasta el corte, anualizados con 52.
- No confundir IR 12M con IR YTD ni usar el YTD para construir el IR de 12 meses.

### Alpha

- Alpha 1 año: retorno de la cartera menos retorno del benchmark/peer group a 1 año.
- Alpha YTD: retorno YTD de la cartera menos retorno YTD del benchmark/peer group.
- En la implementación live/custom existente, el benchmark YTD se calcula sobre la media de `[BCI, peers]`, porque así estaba definido en el ZIP original. No cambiarlo silenciosamente.

### YTD calendario

El YTD debe representar el retorno de año calendario, no el retorno desde mayo ni una ventana de 52 semanas. La implementación v5 conserva un baseline validado y, para un corte personalizado, usa el factor relativo entre el nivel del corte y el nivel del baseline:

```text
YTD_corte = (1 + YTD_baseline) * factor_corte_vs_baseline - 1
```

`_correct_custom_calendar_ytd()` aplica además la media de factores al benchmark. Esta es la zona que debe auditarse con datos reales para resolver el 31-07; no sustituirla por una fórmula nueva sin una prueba que explique el desfase.

### Solapes y anchors

`_merge_return_segments()` considera los cierres existentes como anclas autoritativas. Si una cartola vuelve a traer el 31-07, no debe capitalizar de nuevo ese cierre. Si se encuentra una discrepancia, comparar primero el nivel existente y el retorno de la cartola, en vez de borrar el anchor validado.

## 9. Referencias numéricas conocidas

`panel_metrics_reference.json` contiene, entre otros:

```text
CD Activa (8640), fecha 2026-08-21:
  Retorno YTD             0.13879493848046365
  Retorno benchmark YTD  0.1336972110805319
  Alpha YTD              0.005097727399931751
  TE EWMA anual          0.016220107071024333
  Information Ratio     1.295009439872904

CP Activa (9060), fecha 2026-08-21:
  Retorno YTD             0.13044497299068003
  Retorno benchmark YTD  0.12212693956339185
  Alpha YTD              0.00831803342728818
  TE EWMA anual          0.01507827852333503
  Information Ratio     1.8781521895336875
```

Auditoría local independiente del proyecto anterior, corte 31-07-2026:

- `CD Activa / 8640`: `0.11663718308130155` (**11,6637%**).
- `CP Activa / 9060`: `0.11004097139211821` (**11,0041%**).

El archivo de auditoría se encontraba fuera de este repo en:
`C:\Users\andre\OneDrive\Escritorio\panel_riesgo_mercado\AUDITORIA_ALPHA_YTD_POR_FONDO.md`.

## 10. Railway y producción

Identificadores usados:

```text
projectId:       d25a0feb-4a01-4b18-a7c7-e461a963d8f4
environmentId:   5842466c-b54d-4984-adfd-ec75d769efaf
serviceId:       a6d2baec-0c80-4abc-92fc-7f3f43d5fc9e
service:         bci-tracking-error-final
```

El deploy correcto se forzó con `serviceInstanceDeployV2(commitSha=...)` al commit `e49101f`. Comando equivalente (requiere sesión autenticada en Railway CLI):

```powershell
npx.cmd -y @railway/cli api 'mutation Deploy($environmentId:String!,$serviceId:String!,$commitSha:String){serviceInstanceDeployV2(environmentId:$environmentId,serviceId:$serviceId,commitSha:$commitSha)}' `
  --var environmentId=5842466c-b54d-4984-adfd-ec75d769efaf `
  --var serviceId=a6d2baec-0c80-4abc-92fc-7f3f43d5fc9e `
  --var commitSha=e49101f --compact
```

Deploy observado:

```text
deployId: 46aec222-67c1-4de7-bb74-38bb33aff74c
status:   SUCCESS
commit:   e49101f
```

### Verificaciones ya realizadas

- `GET /login`: HTTP 200.
- Dashboard de producción: cargó el hero y `canvas#risk3d` (`canvas: true`).
- Selector de peers: se observaron 5 opciones para CP Activa.
- `GET /?fondo=9060&peer_config=1&fecha_corte=2026-07-31`: cargó con fecha de corte 31-07.
- Tests unitarios: **5/5 OK**.
- Pycompile de `flask_app_v2.py`, `flask_app_v4.py`, `flask_app_v5.py`, `quota_update.py`: OK.
- `git diff --check`: OK, sólo advertencias de line endings de Git.

### Health actual

La ruta `/health` fue consultada después del deploy y respondió HTTP 500 con `ok: false` porque el runtime recién recreado no tenía una cartola posterior al 21-08:

```json
{
  "ok": false,
  "runtime": "flask_app_v5_authoritative_ytd",
  "cp_activa_fecha": "2026-08-21",
  "cp_activa_ytd": 0.13044497299068003,
  "cp_activa_alpha_ytd": 0.008318033427288096,
  "cp_activa_ir": 1.8781521895336875
}
```

Esto es un **health gate de datos**, no un fallo de build ni de Flask. Para que `/health` vuelva a 200, hay que cargar una cartola válida con fecha suficientemente reciente y cobertura >= 80% de los RUN esperados, o configurar/preservar un volumen runtime.

## 11. Reproducción y plan de diagnóstico para Claude

Ejecutar desde la raíz del repo:

```powershell
cd C:\Users\andre\OneDrive\Escritorio\bci-tracking-error-peers
git status --short --branch
git log --oneline --decorate -12
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m py_compile flask_app_v2.py flask_app_v4.py flask_app_v5.py quota_update.py
```

Para reproducir un escenario, usar el mismo conjunto de peers que aparece en el query string y el fondo correcto. Ejemplo conceptual:

```python
import pandas as pd
import flask_app_v5 as v5

cfg = v5.base.config_by_name("CD Activa")[1]
print(cfg["bci"], cfg["peers"])
print(v5.compute_reference(
    "CD Activa",
    peer_runs=[str(x) for x in cfg["peers"]],
    cutoff_date=pd.Timestamp("2026-07-31"),
))
```

Checklist de investigación:

1. Confirmar el RUN que el usuario llama “Activa” y copiar la URL exacta del escenario.
2. Leer `fondos_config.json`, `panel_metrics_reference.json` y el número de peers efectivo.
3. Revisar los archivos runtime (si existen) y contar duplicados por `fecha/RUN/serie` y por `fecha/RUN` en gross returns.
4. Para RUN 8640 y/o 9060, imprimir cuota/nivel del 30-06, 31-07, 01-08 y 21-08; comprobar si el 31-07 está presente tanto en el histórico embebido como en la cartola.
5. Calcular el retorno bruto diario del RUN y componerlo una sola vez. Un gap >7 días debe quedar enmascarado.
6. Comparar el YTD directo de niveles con el resultado de `_correct_custom_calendar_ytd()`.
7. Repetir con el P-group antiguo y el nuevo para separar un cambio de composición (4 vs 5 peers en CP Activa) de un error de duplicación.
8. Crear una prueba mínima para el caso exacto antes de tocar producción.
9. Después de una corrección, ejecutar tests, levantar Flask localmente y probar `/login`, dashboard, selector de fecha, selector de peers y actualización con una fecha ya cacheada (sin CAPTCHA).
10. Sólo si los valores quedan reconciliados, desplegar al mismo servicio mediante `serviceInstanceDeployV2(commitSha=<HEAD>)`, volver a consultar `/health` y abrir la URL pública.

No descargar otra cartola sólo para “ver si se arregla”: la fecha 31-07 ya está disponible en la evidencia local y el objetivo es explicar el cálculo.

## 12. Restricciones explícitas

- No cambiar las fórmulas de retorno YTD, alpha, IR, IR YTD ni tracking error sin una causa demostrada y autorización.
- No eliminar Three.js ni el hero `#risk3d`.
- No volver a mostrar sólo los peers seleccionados: el selector debe seguir mostrando el universo completo configurable.
- No reintroducir el RUN `10331`.
- No convertir gaps grandes en retornos diarios.
- No aplicar dos veces el cierre 31-07 ni sobrescribir anchors históricos validados sin evidencia.
- No mezclar CD Activa y CP Activa al validar el número esperado.
- No sustituir el P-group nuevo del Excel por el antiguo sin dejarlo explícito.
- No abrir CAPTCHA para una fecha ya persistida.
- No afirmar que `/health` está sano si faltan datos runtime; distinguir salud de código y salud de cobertura de datos.
- No usar un valor observado de 11,88% como referencia correcta sólo porque aparece en la UI.

## 13. Nota de tokens

No se consumió ningún crédito de reset ni se hizo una recuperación automática de tokens. Para continuar de forma económica, Claude debería empezar por inspección local y pruebas deterministas; dejar Railway/CAPTCHA para el final, sólo si los datos locales no permiten resolver la discrepancia.
