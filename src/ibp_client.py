"""Cliente OData v2 para SAP IBP, acotado a Key Figures (sin datos maestros).

Dos comunicaciones cubren el ciclo completo:

- ``SAP_COM_0720`` -> ``/sap/opu/odata/IBP/PLANNING_DATA_API_SRV`` (primario).
  Lee y ESCRIBE key figures de una Planning Area. La escritura es asíncrona:
  se sube un lote a ``{PA}Trans``, IBP procesa la transacción en background,
  y se consulta el estado con ``getExportResult`` / ``{PA}Message``.
- ``SAP_COM_0143`` -> ``/sap/opu/odata/IBP/EXTRACT_ODATA_SRV`` (fallback de
  lectura). Solo extracción; no soporta escritura.

Referencias verificadas:
- SAP-samples/integrated-business-planning-external-forecasting-python
  (carpeta Working_with_KFData) — forma real del payload de escritura
  (Transactionid, AggregationLevelFieldsString, DoCommit, Nav{PA}) y el
  flujo CSRF -> POST {PA}Trans -> poll getExportResult/{PA}Message.
- SAP Help Portal, "Extracting Key Figure Data with OData Output"
  (EXTRACT_ODATA_SRV): $select obligatorio, $filter con PERIODID{N}_TSTAMP,
  límite de 50.000 registros por request, paginación con $top/$skip.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import requests

PLANNING_SVC = "sap/opu/odata/IBP/PLANNING_DATA_API_SRV"
EXTRACT_SVC = "sap/opu/odata/IBP/EXTRACT_ODATA_SRV"

MAX_ROWS_PER_TRANSACTION = 5000
MAX_RECORDS_PER_READ_PAGE = 5000


class IBPError(RuntimeError):
    """Error de comunicación o de negocio devuelto por SAP IBP."""


@dataclass
class WriteResult:
    transaction_id: str
    status: str  # "PROCESSED" | "PROCESSED_WITH_ERRORS" | "TIMEOUT" | "FAILED"
    rows_sent: int
    messages: list[dict] = field(default_factory=list)


class IBPKeyFigureClient:
    """Lee y escribe Key Figures en una Planning Area de SAP IBP.

    ``tenant_url`` es el host del tenant (sin esquema), p.ej.
    ``my12345-api.scmibp.ondemand.com``. Las credenciales viajan solo por
    HTTPS Basic Auth y nunca se persisten en disco por este cliente.
    """

    def __init__(
        self,
        tenant_url: str,
        user: str,
        password: str,
        planning_area: str,
        timeout: int = 60,
        verify_ssl: bool = True,
    ):
        self.tenant_url = tenant_url.replace("https://", "").replace("http://", "").rstrip("/")
        self.planning_area = planning_area
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._auth = (user, password)
        self._session = requests.Session()

    def _base(self, svc: str) -> str:
        return f"https://{self.tenant_url}/{svc}"

    # ---------------------------------------------------------------- read

    def _service_document_entity_sets(self, svc: str) -> list[str] | None:
        """GET del documento de servicio (raíz) — lista TODOS los entity sets reales,
        sin asumir un nombre fijo como 'PlanningAreaSet' (que no existe en todas las
        versiones/tenants de PLANNING_DATA_API_SRV)."""
        r = self._session.get(
            f"{self._base(svc)}/",
            params={"$format": "json"},
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not r.ok:
            raise IBPError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        raw = data.get("d", {}).get("EntitySets") or data.get("EntitySets") or []
        return [es if isinstance(es, str) else (es.get("name") or es.get("EntitySetName") or "") for es in raw]

    def test_connection(self) -> dict[str, Any]:
        """Prueba conectividad y lista Planning Areas visibles (0720 primario, 0143 fallback).

        No asume un entity set fijo tipo 'PlanningAreaSet' — pide el documento de
        servicio (raíz) de cada API y detecta Planning Areas reales como los
        entity sets que tienen un hermano '{nombre}Trans' (convención confirmada:
        cada PA expone {PA}, {PA}Trans, {PA}Message).
        """
        try:
            entity_sets = self._service_document_entity_sets(PLANNING_SVC)
            names = set(entity_sets)
            planning_areas = sorted(n for n in names if f"{n}Trans" in names)
            return {"ok": True, "service": "SAP_COM_0720", "planning_areas": planning_areas, "all_entity_sets": entity_sets}
        except (requests.RequestException, IBPError) as exc:
            svc0720_error = str(exc)

        try:
            entity_sets = self._service_document_entity_sets(EXTRACT_SVC)
            names = set(entity_sets)
            planning_areas = sorted(n for n in names if f"{n}Trans" in names)
            return {
                "ok": True, "service": "SAP_COM_0143", "planning_areas": planning_areas,
                "all_entity_sets": entity_sets, "svc0720_error": svc0720_error,
            }
        except (requests.RequestException, IBPError) as exc:
            return {"ok": False, "service": None, "planning_areas": [], "error": f"{svc0720_error} | {exc}"}

    def read_key_figure(
        self,
        select_fields: list[str],
        filter_str: str | None = None,
        use_planning_api: bool = True,
        max_pages: int = 200,
        max_rows: int | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> pd.DataFrame:
        """Lee una o más key figures como DataFrame ancho (una columna por field seleccionado).

        ``select_fields`` debe incluir las dimensiones (PRDID/CUSTID/LOCID),
        la columna de período (p.ej. ``PERIODID1_TSTAMP``) y la(s) key
        figure(s) a extraer. El filtro de período/versión va en ``filter_str``
        siguiendo la sintaxis de ``$filter`` de IBP (ver docs/ibp-extract-odata-api.md).

        La paginación vía ``$skip`` es secuencial (una request por página de
        hasta 5000 filas) — sin acotar con ``filter_str`` (rango de fechas)
        o ``max_rows``, una key figure de grano fino (p.ej. diario) sobre
        muchas combinaciones puede tardar minutos y no dar ninguna señal de
        vida. ``on_page(rows_so_far, page_number)`` se llama después de cada
        página para que el caller pueda mostrar progreso real.
        """
        svc = PLANNING_SVC if use_planning_api else EXTRACT_SVC
        base_url = f"{self._base(svc)}/{self.planning_area}"

        rows: list[dict] = []
        skip = 0
        for page_num in range(1, max_pages + 1):
            params = {
                "$select": ",".join(select_fields),
                "$format": "json",
                "$top": MAX_RECORDS_PER_READ_PAGE,
                "$skip": skip,
            }
            if filter_str:
                params["$filter"] = filter_str
            r = self._session.get(
                base_url,
                params=params,
                auth=self._auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if not r.ok:
                raise IBPError(f"Lectura de key figure falló (HTTP {r.status_code}): {r.text[:300]}")
            page = r.json().get("d", {}).get("results", [])
            rows.extend(page)
            if on_page:
                on_page(len(rows), page_num)
            if max_rows and len(rows) >= max_rows:
                break
            if len(page) < MAX_RECORDS_PER_READ_PAGE:
                break
            skip += MAX_RECORDS_PER_READ_PAGE
        else:
            raise IBPError(f"read_key_figure: se alcanzó max_pages={max_pages} sin agotar resultados")

        df = pd.DataFrame(rows)
        return df.drop(columns=[c for c in df.columns if c.startswith("__")], errors="ignore")

    # --------------------------------------------------------------- write

    def _fetch_csrf(self, svc: str = PLANNING_SVC) -> str:
        r = self._session.get(
            f"{self._base(svc)}/$metadata",
            auth=self._auth,
            headers={"x-csrf-token": "fetch"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        token = r.headers.get("x-csrf-token")
        if not r.ok or not token:
            raise IBPError(f"No se pudo obtener CSRF token (HTTP {r.status_code}): {r.text[:300]}")
        return token

    def write_key_figures(
        self,
        rows: list[dict],
        field_string: list[str],
        do_commit: bool = True,
        poll: bool = True,
        poll_interval_s: int = 5,
        poll_timeout_s: int = 300,
    ) -> list[WriteResult]:
        """Escribe filas de key figures en lotes de hasta ``MAX_ROWS_PER_TRANSACTION``.

        Cada fila es un dict con las dimensiones (PRDID/CUSTID/LOCID), la
        columna de período (p.ej. ``PERIODID1_TSTAMP``) y la(s) key
        figure(s) destino (Forecast, Ex Post), tal como espera IBP en el
        entity set ``{PlanningArea}Trans``. Ver Working_with_KFData/RequestHandler.py
        del repo SAP-samples/integrated-business-planning-external-forecasting-python.
        """
        results: list[WriteResult] = []
        for start in range(0, len(rows), MAX_ROWS_PER_TRANSACTION):
            batch = rows[start:start + MAX_ROWS_PER_TRANSACTION]
            results.append(self._write_batch(batch, field_string, do_commit, poll, poll_interval_s, poll_timeout_s))
        return results

    def _write_batch(
        self,
        batch: list[dict],
        field_string: list[str],
        do_commit: bool,
        poll: bool,
        poll_interval_s: int,
        poll_timeout_s: int,
    ) -> WriteResult:
        csrf = self._fetch_csrf(PLANNING_SVC)
        transaction_id = uuid.uuid4().hex
        payload = {
            "Transactionid": transaction_id,
            "AggregationLevelFieldsString": ",".join(field_string),
            "DoCommit": bool(do_commit),
            f"Nav{self.planning_area}": batch,
        }
        r = self._session.post(
            f"{self._base(PLANNING_SVC)}/{self.planning_area}Trans",
            json=payload,
            auth=self._auth,
            headers={"x-csrf-token": csrf, "Content-Type": "application/json"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if r.status_code != 201:
            raise IBPError(f"Escritura a IBP falló (HTTP {r.status_code}): {r.text[:500]}")

        if not poll:
            return WriteResult(transaction_id, "SUBMITTED", len(batch))

        status, messages = self._poll_transaction(transaction_id, poll_interval_s, poll_timeout_s)
        return WriteResult(transaction_id, status, len(batch), messages)

    def _poll_transaction(self, transaction_id: str, interval_s: int, timeout_s: int) -> tuple[str, list[dict]]:
        result_url = f"{self._base(PLANNING_SVC)}/getExportResult"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            r = self._session.get(
                result_url,
                params={"P_TransactionID": f"'{transaction_id}'", "$format": "json"},
                auth=self._auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if r.ok:
                value = (r.json().get("d", {}).get("results") or [{}])[0].get("Value")
                if value == "PROCESSED":
                    return "PROCESSED", []
                if value and value != "PROCESSING":
                    messages = self._fetch_messages(transaction_id)
                    return value, messages
            time.sleep(interval_s)
        return "TIMEOUT", self._fetch_messages(transaction_id)

    def _fetch_messages(self, transaction_id: str) -> list[dict]:
        r = self._session.get(
            f"{self._base(PLANNING_SVC)}/{self.planning_area}Message",
            params={"$filter": f"Transactionid eq '{transaction_id}'", "$format": "json"},
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not r.ok:
            return []
        return r.json().get("d", {}).get("results", [])
