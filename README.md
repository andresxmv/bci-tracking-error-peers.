# BCI Tracking Error Peers

Dashboard Streamlit para monitorear el **Tracking Error leave-one-out** de fondos BCI frente a sus peer groups con una ventana de 52 semanas.

## Alcance

- 141 fondos distribuidos en 16 categorías.
- 16 fondos BCI identificados dentro de sus respectivos peer groups.
- Tracking Error EWMA anualizado, retorno a 1 año, exceso de retorno, Information Ratio y volatilidad.
- Series históricas de 52 semanas para comparación visual.
- Dashboard por categoría, resumen ejecutivo, matriz Alfa vs Tracking Error y comparador BCI vs peer.
- Exportación del dataset completo a CSV y Excel.

## Ejecución local

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Streamlit Community Cloud

Configura el despliegue con:

- Branch: `main`
- Main file path: `app.py`

Los datos del ZIP están empaquetados de forma comprimida dentro del repositorio, por lo que la aplicación no depende del proyecto local `panel_riesgo_mercado` para funcionar en la nube.

## Metodología

Cada fondo se compara contra el promedio simple de los demás fondos de su misma categoría. El Tracking Error usa EWMA RiskMetrics con λ = 0,94 y anualización por √52.
