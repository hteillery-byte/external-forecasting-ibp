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

    def test_connection(self) -> dict[str, Any]:
        """Prueba conectividad y lista Planning Areas visibles (0720 primario, 0143 fallback)."""
        try:
            r = self._session.get(
                f"{self._base(PLANNING_SVC)}/PlanningAreaSet",
                params={"$format": "json"},
                auth=self._auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if r.ok:
                results = r.json().get("d", {}).get("results", [])
                pas = [
                    row.get("PlanningAreaId") or row.get("PlanningArea") or row.get("PLANNINGAREAID")
                    for row in results
                ]
                pas = [p for p in pas if p]
                out = {"ok": True, "service": "SAP_COM_0720", "planning_areas": pas}
                if results and not pas:
                    # El campo con el ID no calzó con ninguno de los nombres esperados —
                    # se adjunta la fila cruda para diagnosticar el esquema real del tenant.
                    out["raw_sample"] = results[0]
                return out
        except requests.RequestException as exc:
            last_err: Exception | str = exc
        else:
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"

        try:
            r = self._session.get(
                f"{self._base(EXTRACT_SVC)}/",
                params={"$format": "json"},
                auth=self._auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            if r.ok:
                return {"ok": True, "service": "SAP_COM_0143", "planning_areas": []}
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
        except requests.RequestException as exc:
            last_err = exc
        return {"ok": False, "service": None, "planning_areas": [], "error": str(last_err)}

    def read_key_figure(
        self,
        select_fields: list[str],
        filter_str: str | None = None,
        use_planning_api: bool = True,
        max_pages: int = 200,
    ) -> pd.DataFrame:
        """Lee una o más key figures como DataFrame ancho (una columna por field seleccionado).

        ``select_fields`` debe incluir las dimensiones (PRDID/CUSTID/LOCID),
        la columna de período (p.ej. ``PERIODID1_TSTAMP``) y la(s) key
        figure(s) a extraer. El filtro de período/versión va en ``filter_str``
        siguiendo la sintaxis de ``$filter`` de IBP (ver docs/ibp-extract-odata-api.md).
        """
        svc = PLANNING_SVC if use_planning_api else EXTRACT_SVC
        base_url = f"{self._base(svc)}/{self.planning_area}"

        rows: list[dict] = []
        skip = 0
        for _ in range(max_pages):
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
