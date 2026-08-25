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
- **`PERIODID{N}_TSTAMP` para nivel diario**: por convención suele ser
  `PERIODID1_TSTAMP`, pero varía según el Time Profile de la Planning Area
  real — la UI lo deja seleccionable y hay que confirmarlo contra
  `$metadata` en el tenant real del cliente (no verificado con datos reales
  aún, pendiente primera conexión).
- **Selección de modelo `auto`**: TBATS si la serie tiene ≥21 observaciones
  (~3 ciclos semanales), si no, Gris Estacional (funciona desde 4 obs). Motor
  en `forecast_engine._fit_one`.

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
