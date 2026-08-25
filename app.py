"""Pronóstico masivo (TBATS + Gris Estacional) con lectura/escritura directa
de Key Figures en SAP IBP vía SAP_COM_0720 (primario) / SAP_COM_0143 (fallback lectura).
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.forecast_engine import RunConfig, run_mass_forecast
from src.ibp_client import IBPError, IBPKeyFigureClient
from src.ibp_export import push_to_ibp
from src.ibp_read import read_history
from src.period_format import add_period_label_column, format_period_label, infer_period_granularity

st.set_page_config(page_title="External Forecasting IBP", layout="wide")
st.title("External Forecasting IBP — TBATS y Modelo Gris Estacional")
st.caption(
    "Pronóstico de demanda diaria y Ex Post a nivel masivo (PRDID × CUSTID × LOCID), "
    "con lectura y escritura directa de Key Figures en SAP IBP (SAP_COM_0720 / SAP_COM_0143)."
)

for key, default in {
    "client": None,
    "conn_result": None,
    "history": None,
    "summary": None,
}.items():
    st.session_state.setdefault(key, default)

tab_conn, tab_hist, tab_fc, tab_export = st.tabs(
    ["1 · Conexión IBP", "2 · Histórico", "3 · Pronóstico masivo", "4 · Exportar a IBP"]
)

# ----------------------------------------------------------------- Tab 1
with tab_conn:
    st.subheader("Conexión — Communication Arrangement SAP_COM_0720 / SAP_COM_0143")
    c1, c2 = st.columns(2)
    with c1:
        tenant_url = st.text_input("Tenant URL", placeholder="my12345-api.scmibp.ondemand.com")
        user = st.text_input("Usuario de comunicación")
        password = st.text_input("Password", type="password")
    with c2:
        planning_area = st.text_input("Planning Area")
        verify_ssl = st.checkbox("Verificar certificado SSL", value=True)

    if st.button("Probar conexión", type="primary", disabled=not (tenant_url and user and password and planning_area)):
        client = IBPKeyFigureClient(tenant_url, user, password, planning_area, verify_ssl=verify_ssl)
        with st.spinner("Conectando a IBP..."):
            result = client.test_connection()
        if result["ok"]:
            st.session_state["client"] = client
            st.session_state["conn_result"] = result
        else:
            st.session_state["client"] = None
            st.session_state["conn_result"] = None
            st.error(f"No se pudo conectar: {result.get('error')}")

    conn_result = st.session_state.get("conn_result")
    if st.session_state["client"] and conn_result:
        pa_typed = st.session_state["client"].planning_area
        st.info(f"Sesión activa · Planning Area ingresada = `{pa_typed}` · conectado vía **{conn_result['service']}**")

        if conn_result["service"] == "SAP_COM_0143":
            st.warning(
                "La conexión se estableció por SAP_COM_0143 (fallback), no por SAP_COM_0720. "
                "**La escritura a IBP (Tab 4) requiere SAP_COM_0720** — revisa por qué el arrangement "
                "primario no respondió antes de intentar exportar Forecast/Ex Post."
            )
            if conn_result.get("svc0720_error"):
                st.code(conn_result["svc0720_error"], language=None)

        discovered = conn_result.get("planning_areas") or []
        if discovered:
            match = pa_typed in discovered
            (st.success if match else st.error)(
                f"Planning Areas detectadas por IBP (entity sets con hermano `{{nombre}}Trans`): {discovered}"
                + ("" if match else f" — **`{pa_typed}` no está en esa lista, revisa mayúsculas/nombre exacto**.")
            )
        else:
            st.warning(
                f"No se detectó ningún entity set con el patrón `{{nombre}}Trans` que confirme "
                f"`{pa_typed}` como Planning Area válida."
            )

        with st.expander("Ver todos los entity sets crudos devueltos por el servicio (diagnóstico)"):
            st.write(conn_result.get("all_entity_sets") or "(sin datos)")

# ----------------------------------------------------------------- Tab 2
with tab_hist:
    st.subheader("Leer histórico de demanda diaria desde IBP")
    client: IBPKeyFigureClient | None = st.session_state["client"]
    if not client:
        st.warning("Conéctate a IBP en la pestaña 1 primero.")
    else:
        c1, c2, c3 = st.columns(3)
        with c1:
            hist_kf = st.text_input("Key Figure histórica", value="ZACTUALSQTYDAY")
        with c2:
            period_field = st.selectbox(
                "Columna de período",
                ["PERIODID1_TSTAMP", "PERIODID2_TSTAMP", "PERIODID0_TSTAMP", "PERIODID3_TSTAMP", "PERIODID4_TSTAMP"],
                help=(
                    "La granularidad de cada PERIODIDx (día/semana/mes/...) depende de cómo se configuró "
                    "el Time Profile de ESTA Planning Area — no asumas que PERIODID1 es diario sin "
                    "confirmarlo primero con un rango de fechas chico."
                ),
            )
        with c3:
            conn_result = st.session_state.get("conn_result") or {}
            default_use_0720 = conn_result.get("service") != "SAP_COM_0143"
            use_planning_api = st.checkbox(
                "Usar SAP_COM_0720 (recomendado)",
                value=default_use_0720,
                help=(
                    "Desmarcado automáticamente porque la Tab 1 conectó por el fallback SAP_COM_0143. "
                    "Nota: por 0143 solo se puede LEER — la escritura en Tab 4 sí necesita 0720."
                    if not default_use_0720 else None
                ),
            )

        c4, c5, c6 = st.columns(3)
        with c4:
            uom = st.text_input(
                "Unidad de medida (UOMTOID)",
                value="UMB",
                help=(
                    "Obligatorio para key figures de cantidad en PLANNING_DATA_API_SRV — IBP rechaza "
                    "la lectura sin esto (HTTP 400 'Add property UOMTOID to a filter condition')."
                ),
            )
        with c5:
            date_from = st.date_input("Desde (opcional)", value=None)
        with c6:
            date_to = st.date_input("Hasta (opcional)", value=None)

        st.caption(
            "Acota SIEMPRE el rango de fechas en la primera prueba con una granularidad nueva — "
            "sin acotar, una columna de período fina (diaria o más) sobre todo el histórico puede "
            "traer millones de filas y tardar minutos sin dar señales de vida."
        )

        CATEGORIAS_SMU = [
            "CARNES", "FIAMBRERIA", "FRUTAS Y VERDURAS", "PANADERIA",
            "QUESOS Y HUEVOS", "PGC ALIMENTACION", "PGC NO ALIMENTACION", "TEXTIL HOGAR",
        ]
        cc1, cc2 = st.columns([1, 2])
        with cc1:
            category_field = st.text_input(
                "Nombre del campo (atributo SMUPRODUCT)", value="CATEGORY",
                help="Nombre exacto de la propiedad tal como aparece expuesta en la Key Figure.",
            )
        with cc2:
            categories_selected = st.multiselect(
                "Categorías a incluir (vacío = todas)", CATEGORIAS_SMU,
                help=(
                    "Para TBATS: Frutas y Verduras, Fiambrería, Quesos y Huevos, Carnes. "
                    "Para Gris Estacional: Textil Hogar. Si tu tenant usa otra escritura exacta, "
                    "agrégala en 'Filtro adicional' abajo en vez de acá."
                ),
            )

        filter_str = st.text_input(
            "Filtro adicional ($filter OData, opcional)",
            placeholder="PRDID eq '24317'",
        )
        c7, c8 = st.columns([1, 3])
        with c7:
            max_rows = st.number_input(
                "Límite de filas (corta la lectura al llegar acá)", min_value=0, value=200_000, step=50_000,
                help="0 = sin límite (no recomendado hasta confirmar la granularidad correcta).",
            )
        with c8:
            exclude_zero = st.checkbox(
                "Excluir valores en 0/vacío al leer (recomendado)",
                value=True,
                help=(
                    "IBP suele devolver una fila por cada combinación PRDID-CUSTID-LOCID × período aunque "
                    "la cantidad sea 0 — en tenants grandes eso infla el volumen leído sin agregar "
                    "información real. El motor de pronóstico ya reconstruye los días faltantes con 0 "
                    "localmente (forecast_engine: asfreq('D', fill_value=0.0)) antes de ajustar el "
                    "modelo, así que no se pierde nada para TBATS/Gris Estacional al excluirlos acá. "
                    "Caveat: si un PRDID-CUSTID-LOCID directamente no existe en un período (por ejemplo, "
                    "antes de su lanzamiento), también quedará relleno con 0 en vez de quedar vacío — "
                    "no distingue 'sin surtido' de 'vendió cero'."
                ),
            )

        # UOMTOID debe ir al inicio del $filter, sin paréntesis (regla de IBP para atributos
        # de conversión). El grupo de categorías va entre paréntesis para que el 'or' interno
        # no se combine mal con los 'and' que lo rodean.
        filter_parts = [f"UOMTOID eq '{uom}'"] if uom else []
        if categories_selected and category_field:
            or_group = " or ".join(f"{category_field} eq '{c}'" for c in categories_selected)
            filter_parts.append(f"({or_group})")
        if date_from:
            filter_parts.append(f"{period_field} ge datetime'{date_from.isoformat()}T00:00:00'")
        if date_to:
            filter_parts.append(f"{period_field} le datetime'{date_to.isoformat()}T00:00:00'")
        if exclude_zero:
            filter_parts.append(f"{hist_kf} gt 0")
        if filter_str:
            filter_parts.append(filter_str)
        combined_filter = " and ".join(filter_parts)

        if st.button("Leer histórico", type="primary", disabled=not hist_kf):
            progress_box = st.empty()

            def _on_page(rows_so_far: int, page_num: int) -> None:
                progress_box.info(f"Leyendo... página {page_num} · {rows_so_far:,} filas acumuladas")

            try:
                extra_select = [category_field] if (categories_selected and category_field) else None
                history = read_history(
                    client, hist_kf, period_field, combined_filter or None, use_planning_api,
                    max_rows=(max_rows or None), on_page=_on_page, extra_select_fields=extra_select,
                )
                progress_box.empty()
                if max_rows and len(history) >= max_rows:
                    st.warning(
                        f"Se cortó la lectura al llegar al límite de {max_rows:,} filas — "
                        "el histórico real puede ser más grande. Acota con Desde/Hasta o sube el límite."
                    )
                st.session_state["history"] = history
            except IBPError as exc:
                progress_box.empty()
                st.error(str(exc))

        history = st.session_state["history"]
        if history is not None and not history.empty:
            n_combos = history[["PRDID", "CUSTID", "LOCID"]].drop_duplicates().shape[0]
            granularity = infer_period_granularity(history["FECHA"])
            granularity_label = {
                "day": "diaria", "week": "semanal", "month": "mensual",
                "quarter": "trimestral", "year": "anual",
            }[granularity]
            st.success(
                f"{len(history):,} filas · {n_combos:,} combinaciones PRDID-CUSTID-LOCID · "
                f"rango {history['FECHA'].min().date()} a {history['FECHA'].max().date()} · "
                f"granularidad detectada: **{granularity_label}**"
            )
            preview = add_period_label_column(history.head(200))
            st.dataframe(
                preview[["PRDID", "CUSTID", "LOCID", "PERÍODO", "CANTIDAD", "FECHA"]],
                use_container_width=True,
            )

# ----------------------------------------------------------------- Tab 3
with tab_fc:
    st.subheader("Ejecutar pronóstico masivo")
    history = st.session_state["history"]
    if history is None or history.empty:
        st.warning("Lee el histórico en la pestaña 2 primero.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            model_choice = st.selectbox(
                "Modelo",
                ["auto", "tbats", "seasonal_grey"],
                help=(
                    "auto: TBATS si hay >=21 observaciones NO-CERO con densidad razonable (>=15% de "
                    "los días), Gris Estacional si hay poca historia, o se omite como demanda "
                    "intermitente (ya cubierta por Croston nativo de IBP) si ninguna aplica."
                ),
            )
        with c2:
            horizon_days = st.number_input("Horizonte de pronóstico (días)", min_value=1, max_value=365, value=14)
        with c3:
            season_length = st.number_input("Largo de estación (Gris Estacional)", min_value=2, max_value=31, value=7)
        with c4:
            n_jobs = st.number_input("Procesos en paralelo", min_value=1, max_value=16, value=1)

        c5, c6 = st.columns(2)
        with c5:
            tbats_fast = st.checkbox(
                "TBATS modo rápido (recomendado a nivel masivo)",
                value=True,
                help=(
                    "Fija la forma del modelo (sin Box-Cox, tendencia no amortiguada, sin ARMA) en vez de "
                    "hacer grid search completo. Sin esto, cada combinación puede tardar 1-2+ minutos y no "
                    "escala a miles de combinaciones. Desactivar solo para analizar una combinación puntual."
                ),
            )
        with c6:
            annual_seasonality = st.checkbox(
                "TBATS: incluir estacionalidad anual (365.25 días)",
                value=False,
                help=(
                    "Requiere >= ~2 años de historia por combinación para activarse (si no hay "
                    "suficiente, se ignora sola y queda solo semanal). Medido con modo rápido sobre "
                    "una serie de 3 años: ~4.2s/combinación solo semanal vs. ~15.9s/combinación con "
                    "anual (~3.8x más lento). A 150.000 combinaciones eso es la diferencia entre horas "
                    "y semanas de cómputo — actívalo solo si de verdad necesitas que TBATS capture "
                    "Navidad/temporadas en vez de dejarlo para los modelos de mediano/largo plazo."
                ),
            )

        if st.button("Ejecutar pronóstico masivo", type="primary"):
            seasonal_periods = (7, 365.25) if annual_seasonality else (7,)
            cfg = RunConfig(
                model=model_choice,
                horizon_days=int(horizon_days),
                season_length=int(season_length),
                seasonal_periods_tbats=seasonal_periods,
                n_jobs=int(n_jobs),
                tbats_fast=tbats_fast,
            )
            with st.spinner(f"Ajustando modelos para {history[['PRDID','CUSTID','LOCID']].drop_duplicates().shape[0]} combinaciones..."):
                summary = run_mass_forecast(history, cfg)
            st.session_state["summary"] = summary

        summary = st.session_state["summary"]
        if summary is not None:
            st.markdown("### Resultados")
            m1, m2, m3 = st.columns(3)
            m1.metric("Ex Post generado", f"{len(summary.ex_post_df):,} filas")
            m2.metric("Forecast generado", f"{len(summary.forecast_df):,} filas")
            m3.metric("Combinaciones con error", f"{len(summary.errors_df):,}")

            if not summary.model_usage.empty:
                st.write("**Uso de modelos por combinación:**")
                st.bar_chart(summary.model_usage)

            if not summary.errors_df.empty:
                with st.expander(f"Ver {len(summary.errors_df)} combinaciones con error"):
                    st.dataframe(summary.errors_df, use_container_width=True)

            st.markdown("### Descargar resultados (CSV)")
            st.caption(
                "Útil para revisar el pronóstico antes de escribirlo en IBP (Tab 4), o para correr "
                "el modelo varias veces con distintos parámetros y comparar los CSV entre sí."
            )
            dcol1, dcol2 = st.columns(2)
            with dcol1:
                st.download_button(
                    "Descargar Ex Post (CSV)",
                    summary.ex_post_df.to_csv(index=False).encode("utf-8"),
                    file_name="ex_post.csv",
                    mime="text/csv",
                )
            with dcol2:
                st.download_button(
                    "Descargar Forecast (CSV)",
                    summary.forecast_df.to_csv(index=False).encode("utf-8"),
                    file_name="forecast.csv",
                    mime="text/csv",
                )

            combos = sorted(set(zip(summary.ex_post_df.PRDID, summary.ex_post_df.CUSTID, summary.ex_post_df.LOCID)))
            if combos:
                sel = st.selectbox("Ver detalle de una combinación", combos, format_func=lambda t: " / ".join(t))
                prdid, custid, locid = sel
                actual = history[(history.PRDID == prdid) & (history.CUSTID == custid) & (history.LOCID == locid)]
                ex_post = summary.ex_post_df.query("PRDID == @prdid and CUSTID == @custid and LOCID == @locid")
                fcst = summary.forecast_df.query("PRDID == @prdid and CUSTID == @custid and LOCID == @locid")

                all_dates = pd.concat([actual.FECHA, ex_post.FECHA, fcst.FECHA])
                granularity = infer_period_granularity(all_dates) if not all_dates.empty else "day"
                hover = "%{customdata}<br>%{y:,.1f}<extra>%{fullData.name}</extra>"

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=actual.FECHA, y=actual.CANTIDAD, name="Histórico real", mode="lines",
                    customdata=[format_period_label(d, granularity) for d in actual.FECHA], hovertemplate=hover,
                ))
                fig.add_trace(go.Scatter(
                    x=ex_post.FECHA, y=ex_post.VALUE, name="Ex Post (ajustado)", mode="lines",
                    customdata=[format_period_label(d, granularity) for d in ex_post.FECHA], hovertemplate=hover,
                ))
                fig.add_trace(go.Scatter(
                    x=fcst.FECHA, y=fcst.VALUE, name="Forecast", mode="lines",
                    customdata=[format_period_label(d, granularity) for d in fcst.FECHA], hovertemplate=hover,
                ))
                fig.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, use_container_width=True)

# ----------------------------------------------------------------- Tab 4
with tab_export:
    st.subheader("Escribir Forecast y Ex Post en SAP IBP")
    client = st.session_state["client"]
    summary = st.session_state["summary"]
    if not client:
        st.warning("Conéctate a IBP en la pestaña 1 primero.")
    elif summary is None:
        st.warning("Ejecuta el pronóstico masivo en la pestaña 3 primero.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            forecast_kf = st.text_input("Key Figure destino — Forecast", placeholder="p.ej. ZEXTFORECASTQTY")
        with c2:
            ex_post_kf = st.text_input("Key Figure destino — Ex Post", placeholder="p.ej. ZEXTEXPOSTQTY")
        period_field_out = st.selectbox(
            "Columna de período destino", ["PERIODID1_TSTAMP", "PERIODID2_TSTAMP", "PERIODID0_TSTAMP"], key="period_out"
        )
        do_commit = st.checkbox("Confirmar transacción inmediatamente (DoCommit)", value=True)

        if st.button("Escribir en SAP IBP", type="primary", disabled=not (forecast_kf and ex_post_kf)):
            with st.spinner("Escribiendo Forecast..."):
                try:
                    fc_results = push_to_ibp(client, summary.forecast_df, forecast_kf, period_field_out, do_commit)
                    st.success(f"Forecast: {len(fc_results)} lote(s) enviados.")
                    for r in fc_results:
                        st.write(f"- Transacción `{r.transaction_id}` → **{r.status}** ({r.rows_sent} filas)")
                        if r.messages:
                            st.json(r.messages)
                except IBPError as exc:
                    st.error(f"Error escribiendo Forecast: {exc}")

            with st.spinner("Escribiendo Ex Post..."):
                try:
                    ep_results = push_to_ibp(client, summary.ex_post_df, ex_post_kf, period_field_out, do_commit)
                    st.success(f"Ex Post: {len(ep_results)} lote(s) enviados.")
                    for r in ep_results:
                        st.write(f"- Transacción `{r.transaction_id}` → **{r.status}** ({r.rows_sent} filas)")
                        if r.messages:
                            st.json(r.messages)
                except IBPError as exc:
                    st.error(f"Error escribiendo Ex Post: {exc}")
