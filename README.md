# External Forecasting IBP

Pronóstico de demanda diaria y Ex Post a nivel masivo (por combinación
PRDID × CUSTID × LOCID), usando **TBATS** (corto plazo, multi-estacionalidad)
y el **Modelo Gris Estacional — Seasonal GM(1,1)** (series cortas, ideal para
Textil Hogar / moda), con lectura y escritura directa de Key Figures en SAP
IBP vía los Communication Arrangements `SAP_COM_0720` (primario) y
`SAP_COM_0143` (fallback de lectura).

Modelos matemáticos de forecasting para complementar SAP IBP Advanced Demand.

## Arquitectura

```
app.py                     # UI Streamlit — 5 pasos: conexión, histórico, pronóstico, Test Phase (MAPE), export
src/
├── ibp_client.py          # Cliente OData v2 IBP — lectura + escritura de Key Figures
├── ibp_read.py            # Lee histórico diario y lo normaliza a formato largo
├── ibp_export.py          # Traduce resultados del engine al payload de escritura IBP
├── forecast_engine.py     # Orquestador masivo: agrupa por combo, elige modelo, corre en paralelo
├── backtest.py            # Test Phase Periods (holdout real, MAPE/WMAPE) — terminología SAP IBP
├── period_format.py       # Formato de período estilo IBP ("MAR 2026") para tablas/gráficos
└── models/
    ├── tbats_model.py     # TBATS (paquete `tbats`) — corto plazo / día
    └── seasonal_grey.py   # Seasonal GM(1,1) — implementación propia, series cortas
```

## Instalación

```bash
pip install -r requirements.txt
```

> `tbats==1.1.3` requiere `scikit-learn<1.6` (usa `check_array(force_all_finite=...)`,
> removido en versiones más nuevas de sklearn). Ya fijado en `requirements.txt`.

## Uso

```bash
streamlit run app.py
```

1. **Conexión IBP** — Tenant URL, usuario/password del Communication
   Arrangement, Planning Area. Las credenciales solo viven en memoria de la
   sesión de Streamlit.
2. **Histórico** — nombre técnico de la Key Figure con la demanda diaria
   real, columna de período (`PERIODID{N}_TSTAMP`, confirmar el nivel diario
   contra el `$metadata` de la Planning Area).
3. **Pronóstico masivo** — modelo (`auto` según densidad de observaciones
   reales, ver tabla abajo), horizonte en días, largo de estación, y opción
   de estacionalidad anual en TBATS (ver caveat de rendimiento en `CLAUDE.md`).
4. **Test Phase (MAPE)** — backtest real: reserva los últimos N días como
   holdout, entrena solo con el resto, pronostica a ciegas y mide MAPE/WMAPE
   contra la venta real ya conocida. Mismo concepto que "Test Phase Periods"
   de SAP IBP (Forecast Model → Forecasting Steps) — recomendado por SAP por
   sobre el Ex-Post para elegir el mejor algoritmo.
5. **Exportar a IBP** — nombres de las Key Figures destino (Forecast y Ex
   Post) y escribe ambas vía `SAP_COM_0720` en lotes, con polling de estado.

## Selección de modelo

`RunConfig.model = "auto"` decide por combinación según **observaciones
reales (no-cero)**, no por el largo del período:

| Situación | Modelo |
|-----------|--------|
| ≥ 21 obs. no-cero, densidad ≥ 15% de los días | **TBATS** — captura estacionalidad semanal (y anual, opt-in) |
| Poca historia pero con señal (≥ 4 obs. no-cero) | **Gris Estacional GM(1,1)** — funciona desde 4 observaciones |
| Ni una cosa ni la otra | **Se omite** — demanda intermitente, ya cubierta por Croston nativo de IBP Advanced Demand |

También se puede forzar un modelo único para todo el batch.

## Documentación técnica

- `docs/odata-integration.md` — flujo completo de lectura/escritura de Key
  Figures, verificado contra el repo oficial SAP-samples
  `integrated-business-planning-external-forecasting-python`.
- `docs/ibp-extract-odata-api.md` — parámetros `$select`/`$filter`/paginación
  de `EXTRACT_ODATA_SRV` (fallback de lectura).

## Tests

```bash
pytest tests/
```
