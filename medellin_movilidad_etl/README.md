# ETL Movilidad Medellín — Siniestralidad vial + cámaras de tránsito

Pipeline **Extract → Transform → Load** que descarga automáticamente datos abiertos de la
Alcaldía de Medellín, depura el ruido y guarda la información limpia en Parquet + una bodega
DuckDB lista para Power BI, pandas o SQL. Solo cubre el **Distrito de Medellín** (16 comunas + 5 corregimientos).

```
                ┌──────────────────────── EXTRACT (requests · Selenium de respaldo) ────────────────────────┐
 MEData (DKAN)  │ víctimas · incidentes · motos · aforos · velocidades · comparendos   → data/raw/<fuente>/  │
 GeoMedellín    │ cámaras CCTV · fotomultas FDT · ARS · cruces semafóricos · comunas  → data/raw/arcgis/     │
 Open-Meteo     │ lluvia / temperatura / visibilidad horaria (ERA5)                   → data/raw/clima/      │
 SIMM CCTV      │ foto en vivo de 81 cámaras (cada 5 min)                             → data/camaras/        │
                └──────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                                           ▼
                TRANSFORM  limpieza (texto, fechas, coords, categorías, duplicados) + cuarentena
                           enriquecimiento: comuna por polígono, cámara más cercana, lluvia, festivos, franjas
                                                           ▼
                LOAD       data/clean/*.parquet · data/warehouse/movilidad_medellin.duckdb · reporte de calidad
```

## Fuentes

| Fuente | Qué aporta | Acceso |
|---|---|---|
| [Víctimas en incidentes viales](https://medata.app.medellin.gov.co/dataset/1-023-25-000360) (MEData `1-023-25-000360`) | **Dataset principal**: 1 fila por víctima (gravedad, edad, sexo, condición, coordenadas) | API DKAN `/api/dataset-fast/{id}` → CSV |
| Incidentes viales (`1-023-25-000094`) | 1 fila por incidente (clase, gravedad, diseño de vía). Se une a víctimas por `radicado` | igual |
| Incidentes con motos, Aforos vehiculares, Velocidad y tiempo de viaje GT | Exposición: volumen y velocidad por corredor | igual (transformación genérica) |
| Velocidad e intensidad, Comparendos, Parque automotor | Opcionales (pesados) — activar en `config.yaml` | igual |
| GeoMedellín ArcGIS `transporte/VC_Infraest_Gestion_Transito` | **Cámaras de circuito cerrado (81)**, fotodetección (50, con qué infracción detecta y vel. máx.), apoyo a red semafórica (67), cruces semafóricos (724) | ArcGIS REST `query?f=geojson` |
| GeoMedellín `transporte/VM_Accidentes` (capa *Siniestralidad*) | **Siniestros 2019-2025** georreferenciados (heridos y solo daños), actualizados de forma constante: extiende la serie más allá de 2021 | ArcGIS REST |
| GeoMedellín `VC_Limite_Politico_Admtivo` | Polígonos de comunas y barrios → validación espacial | ArcGIS REST |
| [Cámaras CCTV del SIMM](https://www.medellin.gov.co/simm/camaras-de-circuito-cerrado) | Imagen en vivo 1280×720 por cámara (`/simm/camaras-cctv/imagen{N}.jpg`) | HTTP |
| Open-Meteo Archive (ERA5) | Lluvia, temperatura, humedad, visibilidad **por hora** | API sin llave |
| Librería `holidays` | Festivos de Colombia (ley Emiliani) | local |

Los tableros del Observatorio (Power BI) muestran los mismos datos de siniestralidad; aquí se toman
directo de las fuentes abiertas porque Power BI embebido no permite descarga programática estable.

## Ruido detectado en las fuentes (perfilado sobre los archivos completos) y cómo se trata

**Víctimas (`Mede_Victimas_inci.csv`, 54 MB)**

| Problema | Magnitud real | Regla |
|---|---|---|
| CSV rellenado hasta 1.048.576 filas (límite de Excel) | **812.475 filas vacías** de 1.048.317 (77 %) | se eliminan |
| Duplicados exactos | 197 | a cuarentena |
| Sin coordenadas | 20.845 | se conservan, bandera `sin_coordenada` |
| Coordenadas fuera de Medellín / lat-lon invertidas | 2.116 | invertidas → se corrigen; resto → se anulan; si además no hay comuna → cuarentena |
| Coma decimal (`"6,2669"`) | 100 % | se convierte a float |
| Edad 0, >100 o "Sin Inf" | 785 + 554 + 1.906 | → nulo, `grupo_edad` recalculado |
| Variantes: `Sin Inf`/`Sin inf`, `Acompañante de Motocicleta`/`…motocicleta`, `Sep`/`Sept` | varios miles | se unifican a un valor canónico |
| Comuna "Sin Inf" | 21.726 | se imputa por polígono si hay coordenada (`comuna_imputada_geo`) |
| Mojibake en el datastore (`MoscÃº`, `PeatÃ³n`) | — | `ftfy` |

**Incidentes (`incidentes_viales.csv`, 62 MB)**

| Problema | Magnitud real | Regla |
|---|---|---|
| **Coordenada comodín** `[-75.7037, 6.2214]` asignada cuando no se georreferenció | **19.613** incidentes en un mismo punto | se anula (`coord_comodin`) |
| Año con `\r` literal pegado (`2019\r`) | 26.760 | se extraen dígitos |
| Escapes hexadecimales literales (`Solo da\xF1os`, `Bel\xE9n`) | 20.560+ | se decodifican |
| `Caida Ocupante` / `Caída de Ocupante` / `Caida de Ocupante` | 4 variantes | → `Caída de ocupante` |
| Comuna `0`, `In`, `AU`, `No Georef`, `Sin Inf`, vacía | ~20.000 | → nulo, se imputa por polígono |
| Radicado repetido | 34 | a cuarentena |

**Capas de cámaras**: 2 cámaras ARS con geometría vacía y nombre en blanco; 11 CCTV sin enlace de
foto o con enlace roto (`imagen.jpg`); el campo `y_origen_nacional` mezcla dos sistemas de referencia
(se descarta y se usa lat/lon WGS84).

Además se valida cada punto contra el **polígono oficial de comunas**: si cae en Bello, Envigado,
Itagüí, etc. se anula la coordenada; si la comuna declarada difiere de la geográfica queda la bandera
`comuna_inconsistente_geo` y ambas columnas (`comuna`, `comuna_geo`).

Nada se borra en silencio: lo descartado va a `data/quarantine/<fuente>_rechazados.csv` con su
`motivo_rechazo`, cada fila limpia lleva `flags_calidad`, y cada regla deja su conteo en
`data/reports/calidad_datos.md`.

## Modelo de datos (DuckDB / Parquet)

| Tabla | Grano | Columnas clave |
|---|---|---|
| `fact_victima` | víctima | fecha_hora, gravedad, actor_vial, edad, sexo, comuna, lat/lon, **dist_m_camara_cctv/fdt/ars**, **cubierto_*_300m**, **precipitacion_mm, llovia**, es_festivo, franja_horaria, hora_pico, flags_calidad |
| `fact_incidente` | incidente | clase, gravedad, diseño de vía, mismas columnas de contexto |
| `fact_siniestro` | siniestro 2019-2025 | clase, gravedad, comuna, cámara más cercana, lluvia |
| `fact_clima_hora` | hora | lluvia, temperatura, humedad, visibilidad |
| `fact_snapshot_camara` | cámara × captura | estado (ok / sin_senal / congelada / error), brillo, es_noche, ruta_imagen, (conteo YOLO opcional) |
| `dim_camara` | cámara | tipo (CCTV/FDT/ARS), ubicación, comuna, url_snapshot, infracciones que detecta |
| `dim_comuna`, `dim_cruce_semaforico`, `dim_fecha` | | dim_fecha incluye festivo y temporada (Navidad, Feria de las Flores…) |
| `stg_aforos`, `stg_velocidad_gt`, … | fuente | limpieza genérica |

Vistas analíticas listas: `v_victimas_comuna_anio`, `v_victimas_actor_franja`, `v_puntos_criticos`
(celdas de ~110 m con ≥20 víctimas y distancia a la cámara más cercana), `v_efecto_lluvia`,
`v_cobertura_camaras`, `v_incidente_con_victimas`, `v_camaras_cctv_siniestros` (siniestros a 300 m de cada cámara CCTV).

Además se exportan `data/clean/camaras.csv` e `incidentes_para_camaras.csv`, que se cargan en la página web de consulta de cámaras.

## Uso

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows   (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt

python -m etl run                     # todo: extract + transform + load (~3-5 min)
python -m etl run --only victimas comunas camaras_cctv camaras_fdt camaras_ars
python -m etl transform               # re-procesar sin volver a descargar
python -m etl extract --force         # forzar descarga aunque la fuente no haya cambiado
python -m etl camaras --rondas 1      # una foto de cada cámara CCTV
python -m etl camaras                 # captura continua cada 5 min (Ctrl+C para parar)
pytest -q                             # 16 pruebas con muestras reales de las fuentes
```

La extracción es **incremental**: `data/raw/_manifest.json` guarda la fecha `modified` del
catálogo, el tamaño y el hash; si la fuente no cambió no se vuelve a descargar.

### Programarlo (Windows)

`programar_windows.bat` crea dos tareas en el Programador de tareas:
ETL completo cada lunes 6:15 a. m. y captura de cámaras cada 5 minutos entre 6:00 y 22:00.

### Conteo de vehículos en las cámaras (opcional)

En `config.yaml` → `camaras_cctv.conteo_vehiculos: true` y `pip install ultralytics`. Cada foto
pasa por YOLOv8n y se guardan conteos de autos, motos, buses, camiones, bicicletas y personas por
cámara y hora: un aforo propio que se cruza con `fact_victima` por `id_camara_cctv_cercana`.

### Power BI / SQL Server

Power BI: *Obtener datos → Parquet* sobre `data/clean/`. Para SQL Server/PostgreSQL:
`duckdb data/warehouse/movilidad_medellin.duckdb -c "COPY fact_victima TO 'fact_victima.csv'"` o
`pandas.read_parquet(...).to_sql(...)`.

## Limitaciones conocidas

* MEData publica víctimas **2014-2021** e incidentes **2014-2020** aunque la ficha diga "mensual";
  el pipeline toma automáticamente los años nuevos cuando la Alcaldía los publique.
* Las cámaras CCTV entregan una foto fija que se refresca; no hay video ni histórico público, por
  eso el histórico se construye capturando hacia adelante.
* Licencia de los datos: CC BY-SA 4.0 — citar "Alcaldía de Medellín – MEData / GeoMedellín / SIMM".
