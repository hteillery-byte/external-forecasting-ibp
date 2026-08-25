#Forecast — External Forecasting IBP

Pronóstico de demanda diaria y Ex Post a nivel masivo (por combinación
PRDID × CUSTID × LOCID), usando **TBATS** (corto plazo, multi-estacionalidad)
y el **Modelo Gris Estacional — Seasonal GM(1,1)** (series cortas, ideal para
Textil Hogar / moda), con lectura y escritura directa de Key Figures en SAP
IBP vía los Communication Arrangements `SAP_COM_0720` (primario) y
`SAP_COM_0143` (fallback de lectura).

Contexto de negocio completo: `SMU_Forecast_Modelos_Externos.pptx`
(SMU Forecast — Modelos matemáticos de forecasting para complementar SAP IBP
Advanced Demand).

## Arquitectura

```
app.py                     # UI Streamlit — 4 pasos: conexión, histórico, pronóstico, export
src/
├── ibp_client.py          # Cliente OData v2 IBP — lectura + escritura de Key Figures
├── ibp_read.py            # Lee histórico diario y lo normaliza a formato largo
├── ibp_export.py          # Traduce resultados del engine al payload de escritura IBP
├── forecast_engine.py     # Orquestador masivo: agrupa por combo, elige modelo, corre en paralelo
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
3. **Pronóstico masivo** — modelo (`auto` elige TBATS si hay ≥21 días de
   historia, si no, Gris Estacional), horizonte en días, largo de estación.
4. **Exportar a IBP** — nombres de las Key Figures destino (Forecast y Ex
   Post) y escribe ambas vía `SAP_COM_0720` en lotes, con polling de estado.

## Selección de modelo

| Situación | Modelo |
|-----------|--------|
| ≥ 21 días de historia diaria | **TBATS** — captura estacionalidad semanal (y anual con ≥2 años) |
| < 21 días (colecciones nuevas, lanzamientos, Textil Hogar) | **Gris Estacional GM(1,1)** — funciona desde 4 observaciones |

`RunConfig.model = "auto"` aplica esta regla por combinación automáticamente;
también se puede forzar un modelo único para todo el batch.

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
