# CLAUDE.md — External Forecasting IBP
> Repositorio: hteillery-byte/external-forecasting-ibp

---
## Qué es esto

Pronóstico de demanda diaria + Ex Post a nivel masivo (PRDID × CUSTID ×
LOCID) usando **TBATS** y el **Modelo Gris Estacional (Seasonal GM(1,1))**,
con lectura/escritura directa de Key Figures en SAP IBP vía
`SAP_COM_0720`/`SAP_COM_0143`. Origen: presentación de contexto del proyecto
(retail Chile), con modelos matemáticos de forecasting pensados para
complementar SAP IBP Advanced Demand.

**No CSV** — el cliente ya tiene el Communication Arrangement configurado,
así que la app escribe directo por OData. **Solo Key Figures** — no hay
datos maestros que sincronizar (a diferencia de `ibp-optimizer-app`, que sí
necesita PSI/BOM/recursos/clientes completos para el optimizador de supply).

---
## Stack

Python 3.11 · Streamlit (`app.py`) · `tbats` (requiere `scikit-learn<1.6`,
ver nota abajo) · `requests` (cliente OData directo, sin proxy Node —
a diferencia de `ibp-optimizer-app` que corre en browser y necesita el
proxy `api/ibp.js` por CORS/SSRF; acá el cliente Python llama a IBP
directo).

---
## Estructura

```
app.py                     # Streamlit — 6 tabs: conexión / histórico / pronóstico / Test Phase (MAPE) / vista combinada / export
src/
├── ibp_client.py          # IBPKeyFigureClient: read_key_figure, write_key_figures (CSRF + poll)
├── ibp_read.py             # read_history() → DataFrame largo (PRDID,CUSTID,LOCID,FECHA,CANTIDAD)
├── ibp_export.py           # build_write_rows() / push_to_ibp()
├── forecast_engine.py      # run_mass_forecast() — agrupa por combo, auto-selecciona modelo
├── backtest.py             # run_backtest() — Test Phase Periods (holdout real, MAPE/WMAPE)
├── combined_view.py        # build_combined_view() — encadena REAL+EX_POST+TEST_PHASE_FORECAST+FORECAST_FUTURO
├── period_format.py        # add_period_label_column() — formato IBP ("MAR 2026") para tablas/gráficos
└── models/
    ├── tbats_model.py       # fit_and_forecast() — MIN_OBS_FOR_TBATS=21
    └── seasonal_grey.py     # fit_and_forecast() — MIN_OBS_FOR_GM11=4, GM(1,1) propio + índice estacional
```

---
## Decisiones clave verificadas (no asumidas)

- **Payload de escritura IBP**: verificado contra el repo oficial
  `SAP-samples/integrated-business-planning-external-forecasting-python`
  (`Working_with_KFData/RequestHandler.py` + `sample_data.json`) — NO es
  invención. Flujo: CSRF (`$metadata` con `x-csrf-token: fetch`) → POST
  `{PA}Trans` con `{Transactionid, AggregationLevelFieldsString, DoCommit,
  Nav{PA}: [...]}` → poll `getExportResult` → `{PA}Message` si hay errores.
  Ver `docs/odata-integration.md`.
- **`tbats==1.1.3` + `scikit-learn>=1.6` rompe**: `check_array(force_all_finite=...)`
  fue removido. Fijado `scikit-learn<1.6` en `requirements.txt`. Si se
  actualiza `tbats` a una versión que ya no dependa de ese kwarg, se puede
  soltar el pin.
- **`PERIODID{N}_TSTAMP` NO sigue una convención numérica fija** — es
  específico de cómo se configuró el Time Profile de cada Planning Area.
  Confirmado con datos reales del tenant SMUPILOTO: `PERIODID0_TSTAMP` es
  diario, `PERIODID1_TSTAMP` es otra granularidad (mensual, por lo que trajo
  solo fechas de inicio de mes) — el orden NO es "menor número = más fino".
  No asumir nunca sin probar con un rango de fechas chico primero. La
  granularidad real se infiere de los datos (`src/period_format.py
  infer_period_granularity`), no del nombre de columna.
- **Selección de modelo `auto` (fix 2026-08-25)**: enruta por observaciones
  NO-CERO (`nnz`), no por largo del período (`n`) — un combo con 3 ventas
  reales dispersas en 540 días reconstruidos tiene `n=540` pero `nnz=3`, y
  antes del fix eso calificaba para TBATS solo por longitud. Ahora: TBATS si
  `nnz >= min_obs_tbats` (21) Y `nnz/n >= min_nonzero_ratio` (0.15,
  heurística sin validar aún contra datos reales del cliente) → si no,
  Gris Estacional si `nnz >= min_obs_grey` (4) → si no, se marca
  `model_used="intermitente"` y se omite (demanda intermitente ya cubierta
  por Croston/Croston TSB nativo de IBP Advanced Demand, fuera de alcance
  por diseño — ver slide 4 de la presentación de contexto). Motor en
  `forecast_engine._fit_one`.
- **TBATS con estacionalidad anual (365.25 días)**: opt-in vía checkbox en
  Tab 3 (`annual_seasonality`), default **desactivado**. Medido con modo
  rápido sobre una serie sintética de 3 años: **~4.2s/combinación solo
  semanal vs. ~15.9s/combinación con anual (~3.8x más lento)**. A 150.000
  combinaciones (3.000 SKU × 50 tiendas, caso real del cliente) esa
  diferencia es horas vs. semanas de cómputo — no activar por defecto sin
  que el cliente confirme que necesita que TBATS capture estacionalidad
  anual explícitamente (la presentación original scopea TBATS a "día de
  semana + patrón mensual", la anual quedaría mejor en los modelos de
  mediano/largo plazo). Requiere además >= ~2 años de historia por combo
  para activarse — si no hay suficiente, se ignora sola.
- **Alcance real corregido por el cliente (2026-08-25): NO son todas las
  categorías.** Solo 5 de las 8 de la presentación, calzando exacto con las
  slides 9/19: TBATS → Frutas y Verduras, Fiambrería, Quesos y Huevos,
  Carnes. Gris Estacional → Textil Hogar. Escala real: ~450 SKU × ~20
  tiendas ≈ 9.000 combinaciones (no las 150.000 del peor caso inicial) —
  mucho más manejable, incluso con estacionalidad anual si el cliente la
  pide. `CATEGORY` es un atributo de `SMUPRODUCT` expuesto como propiedad
  filtrable directo en la Key Figure (confirmado por el cliente, no dato
  maestro aparte) — Tab 2 tiene un multiselect con las 8 categorías
  conocidas + campo de nombre de propiedad editable (`CATEGORY` por
  defecto) + fallback a "Filtro adicional" si la escritura exacta del
  tenant difiere. El filtro de categorías se agrega también a `$select`
  (`extra_select_fields` en `ibp_read.read_history`) porque IBP exige que
  toda propiedad usada en `$filter` esté también seleccionada.
- **Lectura excluye valores 0/vacío por defecto** (`{kf} gt 0` en el
  `$filter`, checkbox en Tab 2, activado por defecto): IBP devuelve una fila
  por cada combinación × período aunque el valor sea 0, lo que infla mucho
  el volumen en tenants grandes. Es seguro excluirlos porque
  `forecast_engine.run_mass_forecast` ya reconstruye los días faltantes
  localmente con `asfreq('D', fill_value=0.0)` antes de ajustar el modelo —
  no se pierde información real para TBATS/Gris Estacional. Caveat conocido,
  no resuelto: esto no distingue "no estaba asortido" de "vendió cero" (ver
  decisión de no usar `ZPLANNEDPRICEDAY` como filtro de surtido — pendiente
  si el cliente lo pide más adelante).
- **Test Phase Periods (`src/backtest.py`, Tab 4, 2026-08-25)**: terminología
  y mecánica tomadas literal de SAP IBP (campo "Test Phase Periods" en
  Forecasting Steps del Forecast Model — SAP KBA 2701226), NO inventadas.
  Entrena SOLO con historia ANTES del holdout (`_fit_one` de
  `forecast_engine.py`, reusado tal cual — la selección de modelo también
  se decide solo con el train, nunca mirando el test), pronostica a ciegas
  y compara contra el real ya conocido. SAP recomienda esto por sobre el
  Ex-Post para elegir el mejor algoritmo (Ex-Post = ajuste in-sample, puede
  sobreestimar precisión). **MAPE es la métrica oficial pedida por el
  cliente** (contexto: competencia entre consultores — "gana el menor
  MAPE" contra un backtest ene-may 2025, ~151 días). MAPE excluye días con
  real=0 del holdout (indefinido, división por cero) — se cuentan aparte en
  `mape_days_excluded`, nunca se ocultan silenciosamente. WMAPE
  (`sum|error|/sum|real|`, agregado entre combinaciones) se muestra como
  respaldo, no reemplaza al MAPE.
  **Fix 2026-08-25 (bug real encontrado por el cliente)**: la primera
  versión definía el holdout como "los últimos N días de lo que se haya
  cargado" — eso ataba el resultado a cuándo se corre la app (sesión actual
  25-08-2026), no a la ventana de calendario que pide el negocio (ene-may
  2025). `run_backtest(history, test_start, test_end, cfg)` ahora recibe
  **fechas de calendario explícitas** — train = toda la historia cargada
  ANTES de `test_start`; lo que haya después de `test_end` se ignora. Tab 4
  tiene date pickers "Desde"/"Hasta" con default 2025-01-01/2025-05-31
  (los del ejemplo del cliente) en vez de un campo de cantidad de días.
- **Vista combinada (`src/combined_view.py`, Tab 5, 2026-08-25)**: encadena
  en una sola línea de tiempo por combinación REAL → EX_POST (ajuste sobre
  TODOS los meses de entrenamiento, no solo un tramo) → TEST_PHASE_FORECAST
  (el mismo holdout ene-may 2025 de Tab 4) → FORECAST_FUTURO (proyección
  pura, sin real). No reimplementa modelos — solo junta en formato largo
  (PRDID,CUSTID,LOCID,FECHA,SEGMENTO,VALOR) lo que ya producen
  `run_mass_forecast` (Ex Post + Forecast, corrido sobre el histórico
  filtrado a ANTES de la fecha de corte del forecast) y `run_backtest`.
  **Fecha de corte del forecast futuro: 1/06/2026, fija a propósito** — el
  cliente confirmó explícitamente que es una fecha fija para resolver el
  caso puntual actual (no relativa a "hoy"), aunque ya haya pasado respecto
  a la fecha real de la sesión. Escalabilidad/generalización a "hoy
  dinámico" queda pendiente, no pedida todavía.
- **Progreso en vivo para `run_mass_forecast`/`run_backtest` (2026-08-25)**:
  ambas aceptan `on_progress(completadas, total, ultimo_resultado)`,
  llamado tras CADA combinación (secuencial y en paralelo vía
  `as_completed`). Sin esto, un batch de cientos/miles de combinaciones no
  daba ninguna señal hasta terminar del todo — el cliente reportó no saber
  si una corrida de 248 combinaciones (`tbats` modo rápido, `n_jobs=1`,
  ~17 min esperados según el benchmark de 3 años/4.2s por combo) estaba
  viva o congelada. `app.py::make_progress_callback` es el helper
  compartido (barra + tally de modelos usados en vivo + ETA) usado en
  Tabs 3, 4 y 5 — no duplicar esta lógica si se agrega un cuarto lugar que
  corra estas funciones.
- **Bug real de orden cronológico (2026-08-25)**: `ibp_read.read_history` no
  ordenaba las filas devueltas por IBP — la paginación no garantiza orden
  temporal, así que el gráfico de "Histórico real" salía en zigzag (Plotly
  conecta puntos en el orden del DataFrame, no por fecha). Fix: `read_history`
  ordena por `DIM_COLS + ["FECHA"]` antes de devolver — corrige el gráfico Y
  evita un problema latente más serio: `forecast_engine.run_mass_forecast`
  usa `asfreq("D")` sobre el índice de fechas, que asume orden cronológico.
  Los Ex Post/Forecast no se vieron afectados porque nacen de una Serie ya
  ordenada (vía `pd.date_range`/`asfreq`), pero el histórico crudo si podía
  llegar desordenado desde cualquier consumidor.
- **Parámetros de TBATS explícitos, no un "modo rápido" bundleado
  (2026-08-25)**: `fit_and_forecast` reemplazó `fast: bool` por 3 parámetros
  independientes (`use_box_cox`, `use_damped_trend`, `use_arma_errors`) — el
  cliente notó, con razón, que agrupar todo en un checkbox sin explicar el
  costo/beneficio de cada uno no daba una recomendación real. Benchmark real
  (serie sintética 3 años, período semanal, los 3 apagados = baseline ~4s):

  | Parámetro | Costo medido | Beneficio |
  |---|---|---|
  | `use_box_cox` | ~1.6x (~6.4s) | Bajo salvo demanda muy heteroscedástica |
  | `use_damped_trend` | ~2.4x (~9.7s) | Evita extrapolar tendencia sin freno — relevante para el horizonte de 60 días de este proyecto |
  | `use_arma_errors` | **~8x (~32s), el más caro por lejos** | Ayuda más a precisión de 1 paso que a un forecast de semanas |

  **Defaults recomendados y ya seteados**: `use_box_cox=False`,
  `use_damped_trend=True` (el único que vale la pena activar dado el
  horizonte largo), `use_arma_errors=False`. `RunConfig` tiene los 3 campos
  (`tbats_use_box_cox`, `tbats_use_damped_trend`, `tbats_use_arma_errors`)
  con esos mismos defaults. UI: `app.py::tbats_param_controls()` — 3
  checkboxes independientes con el costo/beneficio en el tooltip, usado en
  Tabs 3, 4 y 5 (mismo patrón que `make_progress_callback`, no duplicar).

---
## Key Figures reales del tenant (confirmadas por el cliente)

- **`ZACTUALSQTYDAY`** — demanda histórica diaria real. Precargada como
  default en el campo "Key Figure histórica" de la Tab 2 (`app.py`).
- **`ZPLANNEDPRICEDAY`** — existe en el tenant pero **decisión explícita del
  cliente: no se trae, no se usa** (2026-08-25). Se evaluó usarla para
  filtrar días sin surtido (precio 0/null ≈ producto no asortido, para no
  confundirlo con demanda real cero), pero el cliente pidió no incorporarla
  por ahora. **No reintroducir esta lógica sin que el cliente lo pida
  explícitamente de nuevo.**

---
## Pendiente / no verificado con datos reales

- Nombres técnicos reales de las Key Figures **destino** (Forecast, Ex Post)
  — quedan como inputs de la UI, sin hardcodear.
- Nivel de granularidad real del Planning Area (confirmar `PERIODID{N}`
  diario contra `$metadata`).
- Volumen real de combinaciones PRDID-CUSTID-LOCID — define si
  `RunConfig.n_jobs` necesita paralelismo real (`ProcessPoolExecutor`) en
  producción.

---
## Convenciones de código

- Sin comentarios explicando el qué; solo el porqué cuando no es obvio
  (ver ibp_client.py, forecast_engine.py).
- Credenciales solo en `st.session_state`, nunca a disco.
- `pytest tests/` para los modelos (`seasonal_grey`, `tbats_model`).
