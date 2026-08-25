"""Pronóstico masivo (TBATS + Gris Estacional) con lectura/escritura directa
de Key Figures en SAP IBP vía SAP_COM_0720 (primario) / SAP_COM_0143 (fallback lectura).
"""
from __future__ import annotations

import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.backtest import run_backtest
from src.combined_view import EX_POST, FORECAST_FUTURO, REAL, TEST_PHASE_FORECAST, build_combined_view
from src.forecast_engine import RunConfig, run_mass_forecast
from src.ibp_client import IBPError, IBPKeyFigureClient
from src.ibp_export import push_to_ibp
from src.ibp_read import read_history
from src.period_format import add_period_label_column, format_period_label, infer_period_granularity

def make_progress_callback(progress_bar, progress_text):
    """Callback compartido para run_mass_forecast/run_backtest: barra + conteo
    de modelos usado en vivo (tbats/seasonal_grey/intermitente/error) + ETA."""
    tally: dict[str, int] = {}
    t0 = time.monotonic()

    def _on_progress(done: int, total: int, result) -> None:
        key = result.model_used if result.model_used else "error"
        tally[key] = tally.get(key, 0) + 1
        elapsed = time.monotonic() - t0
        eta_s = (elapsed / done) * (total - done) if done else 0
        progress_bar.progress(done / total)
        tally_str = " · ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        progress_text.info(f"{done:,} / {total:,} combinaciones · {elapsed:.0f}s transcurridos · ETA ~{eta_s:.0f}s · {tally_str}")

    return _on_progress


def tbats_param_controls(key_prefix: str):
    """Los 3 componentes opcionales de TBATS, expuestos por separado (no un
    solo "modo rápido" que los agrupa a ciegas). Costo medido con una serie
    sintética de 3 años, período semanal, ~4s/combo con los 3 apagados
    (ver CLAUDE.md y src/models/tbats_model.py para el detalle)."""
    c1, c2, c3 = st.columns(3)
    with c1:
        use_box_cox = st.checkbox(
            "Box-Cox", value=False, key=f"{key_prefix}_boxcox",
            help="Estabiliza varianza si la dispersión crece con el nivel. Costo ~1.6x (~6.4s/combo). Beneficio bajo salvo demanda muy heteroscedástica.",
        )
    with c2:
        use_damped_trend = st.checkbox(
            "Tendencia amortiguada", value=True, key=f"{key_prefix}_damped",
            help="Evita que la tendencia se extrapole sin freno en horizontes largos. Costo ~2.4x (~9.7s/combo). Recomendado activo para horizontes de varias semanas (como los 60 días de este proyecto).",
        )
    with c3:
        use_arma_errors = st.checkbox(
            "Errores ARMA", value=False, key=f"{key_prefix}_arma",
            help="Modela autocorrelación residual -- ayuda más a precisión de 1 paso que a un forecast de semanas. Costo ~8x (~32s/combo), el más caro de los tres por lejos. Dejar apagado a nivel masivo.",
        )
    return use_box_cox, use_damped_trend, use_arma_errors


def render_total_summary_chart(series_specs: list[tuple[str, pd.DataFrame, str, str]]) -> None:
    """Resumen agregado (todas las combinaciones sumadas por fecha).

    ``series_specs``: lista de (nombre_serie, dataframe, columna_fecha,
    columna_valor). Cada serie se agrupa y suma por fecha antes de graficar
    — vista "de pájaro" del portafolio completo en vez de combo por combo.
    """
    fig = go.Figure()
    any_data = False
    for name, df, date_col, value_col in series_specs:
        if df is None or df.empty:
            continue
        agg = df.groupby(date_col)[value_col].sum().reset_index().sort_values(date_col)
        fig.add_trace(go.Scatter(x=agg[date_col], y=agg[value_col], name=name, mode="lines"))
        any_data = True
    if not any_data:
        st.caption("(sin datos para el resumen total)")
        return
    fig.update_layout(height=400, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, use_container_width=True)


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
    "backtest_summary": None,
    "combined_df": None,
}.items():
    st.session_state.setdefault(key, default)

tab_conn, tab_hist, tab_fc, tab_backtest, tab_combined, tab_export = st.tabs([
    "1 · Conexión IBP", "2 · Histórico", "3 · Pronóstico masivo",
    "4 · Test Phase (MAPE)", "5 · Vista combinada", "6 · Exportar a IBP",
])

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

        st.markdown("**Parámetros de TBATS** (cada uno expuesto por separado — ver costo/beneficio en el `?`)")
        use_box_cox, use_damped_trend, use_arma_errors = tbats_param_controls("fc")
        annual_seasonality = st.checkbox(
            "TBATS: incluir estacionalidad anual (365.25 días)",
            value=False,
            help=(
                "Requiere >= ~2 años de historia por combinación para activarse (si no hay "
                "suficiente, se ignora sola y queda solo semanal). Medido con los 3 parámetros de "
                "arriba apagados sobre una serie de 3 años: ~4.2s/combinación solo semanal vs. "
                "~15.9s/combinación con anual (~3.8x más lento). A escala eso es la diferencia entre "
                "horas y semanas de cómputo — actívalo solo si de verdad necesitas que TBATS capture "
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
                tbats_use_box_cox=use_box_cox,
                tbats_use_damped_trend=use_damped_trend,
                tbats_use_arma_errors=use_arma_errors,
            )
            progress_bar = st.progress(0.0)
            progress_text = st.empty()
            summary = run_mass_forecast(history, cfg, on_progress=make_progress_callback(progress_bar, progress_text))
            progress_bar.empty()
            progress_text.empty()
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

            st.markdown("### Resumen total (todas las combinaciones sumadas por fecha)")
            render_total_summary_chart([
                ("Real histórico (total)", history, "FECHA", "CANTIDAD"),
                ("Ex Post (total)", summary.ex_post_df, "FECHA", "VALUE"),
                ("Forecast (total)", summary.forecast_df, "FECHA", "VALUE"),
            ])

            combos = sorted(set(zip(summary.ex_post_df.PRDID, summary.ex_post_df.CUSTID, summary.ex_post_df.LOCID)))
            if combos:
                sel = st.selectbox("Ver detalle de una combinación", combos, format_func=lambda t: " / ".join(t))
                prdid, custid, locid = sel
                actual = history[(history.PRDID == prdid) & (history.CUSTID == custid) & (history.LOCID == locid)].sort_values("FECHA")
                ex_post = summary.ex_post_df.query("PRDID == @prdid and CUSTID == @custid and LOCID == @locid").sort_values("FECHA")
                fcst = summary.forecast_df.query("PRDID == @prdid and CUSTID == @custid and LOCID == @locid").sort_values("FECHA")

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
with tab_backtest:
    st.subheader("Test Phase Periods — backtest real contra venta ya conocida")
    st.caption(
        "Igual a como SAP IBP define \"Test Phase Periods\" (pestaña Forecasting Steps del "
        "Forecast Model): se reserva una ventana de calendario como set de prueba, se entrena "
        "SOLO con lo anterior, y se pronostica a ciegas hacia esa ventana — el resultado se "
        "compara contra la venta real ya conocida. SAP recomienda esto por sobre el Ex-Post "
        "(ajuste in-sample) para elegir el mejor algoritmo, porque el Ex-Post puede sobreestimar "
        "la precisión. **La ventana es de fechas de calendario fijas, no \"los últimos N días de "
        "hoy\"** — así el resultado no depende de cuándo corras la app."
    )
    history = st.session_state["history"]
    if history is None or history.empty:
        st.warning("Lee el histórico en la pestaña 2 primero.")
    else:
        st.info(
            f"Histórico cargado: {history['FECHA'].min().date()} a {history['FECHA'].max().date()}. "
            "La ventana de test de abajo debe caer DENTRO de este rango — si en la Tab 2 acotaste "
            "'Hasta' antes del fin de la ventana de test, esos días simplemente no van a estar "
            "disponibles acá. Vuelve a leer en Tab 2 con un rango más amplio si hace falta."
        )
        b1, b2 = st.columns(2)
        with b1:
            test_start = st.date_input("Test Phase — Desde", value=pd.Timestamp("2025-01-01").date())
        with b2:
            test_end = st.date_input("Test Phase — Hasta", value=pd.Timestamp("2025-05-31").date())
        if test_start and test_end:
            st.caption(f"Ventana de test: {(test_end - test_start).days + 1} días. El entrenamiento usa toda la historia cargada ANTES de {test_start}.")

        b3, b4, b5 = st.columns(3)
        with b3:
            bt_model_choice = st.selectbox("Modelo", ["auto", "tbats", "seasonal_grey"], key="bt_model")
        with b4:
            bt_season_length = st.number_input("Largo de estación (Gris Estacional)", min_value=2, max_value=31, value=7, key="bt_season")
        with b5:
            bt_n_jobs = st.number_input("Procesos en paralelo", min_value=1, max_value=16, value=1, key="bt_njobs")

        bt_use_box_cox, bt_use_damped_trend, bt_use_arma_errors = tbats_param_controls("bt")
        bt_annual = st.checkbox("TBATS: incluir estacionalidad anual (365.25 días)", value=False, key="bt_annual")

        if st.button("Ejecutar Test Phase", type="primary", disabled=not (test_start and test_end and test_start <= test_end)):
            bt_cfg = RunConfig(
                model=bt_model_choice,
                season_length=int(bt_season_length),
                seasonal_periods_tbats=(7, 365.25) if bt_annual else (7,),
                n_jobs=int(bt_n_jobs),
                tbats_use_box_cox=bt_use_box_cox,
                tbats_use_damped_trend=bt_use_damped_trend,
                tbats_use_arma_errors=bt_use_arma_errors,
            )
            progress_bar = st.progress(0.0)
            progress_text = st.empty()
            backtest_summary = run_backtest(
                history, test_start, test_end, bt_cfg,
                on_progress=make_progress_callback(progress_bar, progress_text),
            )
            progress_bar.empty()
            progress_text.empty()
            st.session_state["backtest_summary"] = backtest_summary

        backtest_summary = st.session_state["backtest_summary"]
        if backtest_summary is not None:
            st.markdown("### Resultados")
            r1, r2, r3 = st.columns(3)
            r1.metric("MAPE (promedio por combinación)", f"{backtest_summary.overall_mape:.1f}%" if backtest_summary.overall_mape is not None else "—")
            r2.metric("WMAPE (ponderado por volumen)", f"{backtest_summary.overall_wmape:.1f}%" if backtest_summary.overall_wmape is not None else "—")
            r3.metric("Combinaciones evaluadas", f"{len(backtest_summary.results):,}")

            st.caption(
                "MAPE es la métrica oficial pedida por el cliente — se calcula excluyendo días del "
                "holdout con venta real = 0 (división indefinida); esos días quedan contados aparte "
                "en 'dias_excluidos_mape'. WMAPE se muestra como respaldo porque no tiene ese problema "
                "y pondera por volumen en vez de tratar igual a un SKU chico que a uno grande."
            )

            summary_df = backtest_summary.summary_df
            st.dataframe(summary_df, use_container_width=True)

            st.download_button(
                "Descargar resultados Test Phase (CSV)",
                summary_df.to_csv(index=False).encode("utf-8"),
                file_name="test_phase_mape.csv",
                mime="text/csv",
            )

            st.markdown("### Resumen total (todas las combinaciones sumadas por fecha)")
            render_total_summary_chart([
                ("Real (holdout, total)", backtest_summary.detail_df, "FECHA", "ACTUAL"),
                ("Forecast ciego (total)", backtest_summary.detail_df, "FECHA", "FORECAST"),
            ])

            combos_ok = summary_df.dropna(subset=["MAPE_%"])[["PRDID", "CUSTID", "LOCID"]]
            if not combos_ok.empty:
                combos_list = list(combos_ok.itertuples(index=False, name=None))
                sel = st.selectbox(
                    "Ver detalle Test Phase de una combinación", combos_list,
                    format_func=lambda t: " / ".join(t), key="bt_detail_sel",
                )
                prdid, custid, locid = sel
                detail = backtest_summary.detail_df.query(
                    "PRDID == @prdid and CUSTID == @custid and LOCID == @locid"
                ).sort_values("FECHA")
                fig_bt = go.Figure()
                fig_bt.add_trace(go.Scatter(x=detail.FECHA, y=detail.ACTUAL, name="Real (holdout)", mode="lines+markers"))
                fig_bt.add_trace(go.Scatter(x=detail.FECHA, y=detail.FORECAST, name="Forecast (a ciegas)", mode="lines+markers"))
                fig_bt.update_layout(height=350, margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig_bt, use_container_width=True)

# ----------------------------------------------------------------- Tab 5
with tab_combined:
    st.subheader("Vista combinada — Real, Ex Post, Test Phase y Forecast futuro en una sola línea de tiempo")
    st.caption(
        "Encadena: Real histórico + Ex Post (ajuste sobre TODOS los meses de entrenamiento) + "
        "Test Phase (forecast ciego contra el holdout de calendario) + Forecast futuro puro (sin "
        "real, proyección desde la fecha de corte). Corre de nuevo el pronóstico y el Test Phase "
        "con las fechas de acá — no reutiliza lo que hayas corrido en las Tabs 3/4 con otra config."
    )
    history = st.session_state["history"]
    if history is None or history.empty:
        st.warning("Lee el histórico en la pestaña 2 primero.")
    else:
        v1, v2 = st.columns(2)
        with v1:
            st.markdown("**Test Phase**")
            v_test_start = st.date_input("Test Phase — Desde", value=pd.Timestamp("2025-01-01").date(), key="v_test_start")
            v_test_end = st.date_input("Test Phase — Hasta", value=pd.Timestamp("2025-05-31").date(), key="v_test_end")
        with v2:
            st.markdown("**Forecast futuro**")
            v_forecast_start = st.date_input("Forecast — Desde", value=pd.Timestamp("2026-06-01").date(), key="v_fc_start")
            v_horizon = st.number_input("Horizonte (días)", min_value=1, max_value=365, value=60, key="v_horizon")

        v3, v4, v5, v6 = st.columns(4)
        with v3:
            v_model = st.selectbox("Modelo", ["auto", "tbats", "seasonal_grey"], key="v_model")
        with v4:
            v_season = st.number_input("Largo de estación (Gris Estacional)", min_value=2, max_value=31, value=7, key="v_season")
        with v5:
            v_njobs = st.number_input("Procesos en paralelo", min_value=1, max_value=16, value=1, key="v_njobs")
        with v6:
            v_annual = st.checkbox("TBATS: estacionalidad anual", value=False, key="v_annual")
        v_use_box_cox, v_use_damped_trend, v_use_arma_errors = tbats_param_controls("v")

        if st.button("Generar vista combinada", type="primary"):
            forecast_start_ts = pd.Timestamp(v_forecast_start)
            training_history = history[history["FECHA"] < forecast_start_ts]
            v_cfg = RunConfig(
                model=v_model,
                horizon_days=int(v_horizon),
                season_length=int(v_season),
                seasonal_periods_tbats=(7, 365.25) if v_annual else (7,),
                n_jobs=int(v_njobs),
                tbats_use_box_cox=v_use_box_cox,
                tbats_use_damped_trend=v_use_damped_trend,
                tbats_use_arma_errors=v_use_arma_errors,
            )
            if training_history.empty:
                st.error(f"No hay histórico cargado antes de {v_forecast_start} — no se puede entrenar.")
            else:
                st.caption("Ex Post + Forecast futuro:")
                pb1, pt1 = st.progress(0.0), st.empty()
                mass_summary = run_mass_forecast(training_history, v_cfg, on_progress=make_progress_callback(pb1, pt1))
                pb1.empty(); pt1.empty()

                st.caption("Test Phase:")
                pb2, pt2 = st.progress(0.0), st.empty()
                backtest_summary = run_backtest(history, v_test_start, v_test_end, v_cfg, on_progress=make_progress_callback(pb2, pt2))
                pb2.empty(); pt2.empty()

                st.session_state["combined_df"] = build_combined_view(training_history, mass_summary, backtest_summary)

        combined_df = st.session_state["combined_df"]
        if combined_df is not None and not combined_df.empty:
            st.markdown("### Resumen total (todas las combinaciones sumadas por fecha)")
            render_total_summary_chart([
                ("Real histórico (total)", combined_df[combined_df.SEGMENTO == REAL], "FECHA", "VALOR"),
                ("Ex Post (total)", combined_df[combined_df.SEGMENTO == EX_POST], "FECHA", "VALOR"),
                ("Test Phase (total)", combined_df[combined_df.SEGMENTO == TEST_PHASE_FORECAST], "FECHA", "VALOR"),
                ("Forecast futuro (total)", combined_df[combined_df.SEGMENTO == FORECAST_FUTURO], "FECHA", "VALOR"),
            ])

            combos = sorted(set(zip(combined_df.PRDID, combined_df.CUSTID, combined_df.LOCID)))
            sel = st.selectbox("Ver combinación", combos, format_func=lambda t: " / ".join(t), key="v_sel")
            prdid, custid, locid = sel
            d = combined_df.query("PRDID == @prdid and CUSTID == @custid and LOCID == @locid").sort_values("FECHA")

            fig_v = go.Figure()
            style = {
                REAL: dict(name="Real histórico", mode="lines", line=dict(color="#4C78A8")),
                EX_POST: dict(name="Ex Post (ajuste entrenamiento)", mode="lines", line=dict(color="#72B7B2", dash="dot")),
                TEST_PHASE_FORECAST: dict(name="Test Phase (forecast ciego)", mode="lines+markers", line=dict(color="#E45756")),
                FORECAST_FUTURO: dict(name="Forecast futuro", mode="lines", line=dict(color="#F58518")),
            }
            for segmento, kwargs in style.items():
                sub = d[d["SEGMENTO"] == segmento]
                if not sub.empty:
                    fig_v.add_trace(go.Scatter(x=sub.FECHA, y=sub.VALOR, **kwargs))
            fig_v.update_layout(height=450, margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig_v, use_container_width=True)

            st.download_button(
                "Descargar vista combinada (CSV, todas las combinaciones)",
                combined_df.to_csv(index=False).encode("utf-8"),
                file_name="vista_combinada.csv",
                mime="text/csv",
            )

# ----------------------------------------------------------------- Tab 6
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
