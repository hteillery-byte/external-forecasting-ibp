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
app.py                     # Streamlit — 4 tabs: conexión / histórico / pronóstico / export
src/
├── ibp_client.py          # IBPKeyFigureClient: read_key_figure, write_key_figures (CSRF + poll)
├── ibp_read.py             # read_history() → DataFrame largo (PRDID,CUSTID,LOCID,FECHA,CANTIDAD)
├── ibp_export.py           # build_write_rows() / push_to_ibp()
├── forecast_engine.py      # run_mass_forecast() — agrupa por combo, auto-selecciona modelo
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
