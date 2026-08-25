# Integración OData con SAP IBP — solo Key Figures

A diferencia de `ibp-optimizer-app` (que lee datos maestros completos: BOM,
recursos, clientes, ubicaciones para el optimizador de supply), esta app
**solo necesita Key Figures** — no hay dimensiones maestras que sincronizar,
porque las combinaciones PRDID-CUSTID-LOCID ya existen en la Planning Area.

## Comunicaciones SAP

| Scenario | Servicio OData | Uso en esta app |
|----------|-----------------|------------------|
| `SAP_COM_0720` | `/sap/opu/odata/IBP/PLANNING_DATA_API_SRV` | **Primario** — lectura del histórico y **escritura** de Forecast/Ex Post |
| `SAP_COM_0143` | `/sap/opu/odata/IBP/EXTRACT_ODATA_SRV` | Fallback de solo lectura (no soporta escritura) |

Ambas requieren un Communication Arrangement creado en IBP con un usuario de
comunicación (Basic Auth). Ver SAP Note 3170544 y SAP Help "Communication
Scenarios in SAP IBP".

## Lectura de key figures

```
GET {tenant}/sap/opu/odata/IBP/PLANNING_DATA_API_SRV/{PlanningArea}
    ?$select=PRDID,CUSTID,LOCID,PERIODID1_TSTAMP,{KF}
    &$filter=PERIODID1_TSTAMP ge datetime'2025-01-01T00:00:00'
    &$format=json&$top=5000&$skip=0
```

- `$select` es obligatorio.
- Límite de 50.000 registros por request → paginar con `$top`/`$skip`
  (`src/ibp_client.py::read_key_figure`, `MAX_RECORDS_PER_READ_PAGE=5000`).
- `PERIODID{N}_TSTAMP` puede ir en `$select`; `PERIODID{N}_REL` solo en
  `$filter`. El nivel diario suele ser `PERIODID1_TSTAMP`, pero **hay que
  confirmarlo contra el `$metadata` de la Planning Area real** — puede variar
  según el Time Profile configurado.
- Detalle completo de restricciones: `docs/ibp-extract-odata-api.md` (heredado
  de `ibp-optimizer-app`, aplica igual a `EXTRACT_ODATA_SRV`).

## Escritura de key figures (Forecast + Ex Post, "a nivel masivo")

Verificado contra el repo oficial
[`SAP-samples/integrated-business-planning-external-forecasting-python`](https://github.com/SAP-samples/integrated-business-planning-external-forecasting-python)
(carpeta `Working_with_KFData`), que documenta el flujo real de
`IBP_Keyfigure_ODataService`:

1. **CSRF token**: `GET {PLANNING_DATA_API_SRV}/$metadata` con header
   `x-csrf-token: fetch` → se guarda el token + cookies de sesión.
2. **POST del lote**: `POST {PLANNING_DATA_API_SRV}/{PlanningArea}Trans` con
   body:
   ```json
   {
     "Transactionid": "<uuid4 hex>",
     "AggregationLevelFieldsString": "PRDID,CUSTID,LOCID,PERIODID1_TSTAMP,{KF}",
     "DoCommit": true,
     "Nav{PlanningArea}": [
       {"PRDID": "...", "CUSTID": "...", "LOCID": "...",
        "PERIODID1_TSTAMP": "2026-08-25T00:00:00", "{KF}": "12.00000"}
     ]
   }
   ```
   Headers: `x-csrf-token`, `Content-Type: application/json`, cookies de (1).
   HTTP 201 = aceptado (la escritura real es asíncrona).
3. **Poll de estado**: `GET {SERVICE_PATH}/getExportResult?P_TransactionID='<id>'&$format=json`
   hasta que `d.results[0].Value == "PROCESSED"`.
4. **Mensajes/errores**: `GET {PlanningArea}Message?$filter=Transactionid eq '<id>'`
   — útil cuando el resultado es `PROCESSED_WITH_ERRORS`.

Implementado en `src/ibp_client.py::IBPKeyFigureClient.write_key_figures`
(lotes de `MAX_ROWS_PER_TRANSACTION=5000` filas por transacción, con poll
automático).

## Por qué no CSV

La versión inicial de este proyecto contempló exportar Forecast/Ex Post a CSV
para carga manual en IBP. Se descartó: el cliente ya tiene el Communication
Arrangement (0720/0143) configurado y las URLs/escenarios habilitados, así
que la app escribe directamente vía OData — sin paso manual intermedio.

## Formato de fila para escritura

`src/ibp_export.py::build_write_rows` arma cada fila con:

- `PRDID`, `CUSTID`, `LOCID` — strings, tal como vienen del histórico leído.
- `{period_field}` — timestamp ISO sin zona horaria (`YYYY-MM-DDT00:00:00`).
- `{kf_name}` — valor numérico como string con 5 decimales (formato del
  ejemplo oficial de SAP-samples).

## Seguridad

- Las credenciales viven solo en `st.session_state` durante la sesión de
  Streamlit — nunca se escriben a disco ni se commitean (`.env.example` es
  solo plantilla).
- `IBPKeyFigureClient` no cachea la contraseña fuera de la instancia en
  memoria del proceso.
