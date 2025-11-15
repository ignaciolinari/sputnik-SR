# sputnik-SR

Proyecto para construir un sistema de recomendación de discos a partir de datos obtenidos en Sputnikmusic.

## Propósito y Responsabilidad

**Este proyecto es exclusivamente educativo.**

- **Fin académico**: Desarrollado para aprender sobre web scraping, procesamiento de datos y sistemas de recomendación.
- **Respeto a la plataforma**: Implementa rate limiting, delays entre requests y scraping ético para no sobrecargar Sputnikmusic.
- **Uso responsable**: Los datos obtenidos son solo para análisis personal y no se redistribuyen.
- **Cumplimiento**: Respeta los términos de servicio del sitio y evita cualquier impacto negativo en su funcionamiento.

## Tabla de contenidos
- [Propósito y Responsabilidad](#propósito-y-responsabilidad)
- [Entorno de desarrollo](#entorno-de-desarrollo)
- [Base de datos](#base-de-datos)
- [Scraper CLI](#scraper-cli)
- [Crawler masivo](#crawler-masivo)
- [Monitoreo y seguimiento](#monitoreo-y-seguimiento)
- [Aplicación web de recomendaciones](#aplicación-web-de-recomendaciones)
- [Sistema de recomendación híbrido](#sistema-de-recomendación-híbrido)
- [Evaluación offline](#evaluación-offline)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Pruebas](#pruebas)
- [Pre-commits](#pre-commits)
- [Variables de entorno](#variables-de-entorno)
- [Notas sobre recomendaciones (heurística actual)](#notas-sobre-recomendaciones-heurística-actual)

## Entorno de desarrollo

1. Crear el entorno (usa `mamba` o `conda` indistintamente):

	```bash
	mamba env create -f environment.yml
	```

2. Activar el entorno:

	```bash
	conda activate sputnik-sr
	```

3. Registrar el kernel para notebooks (opcional, solo si usas Jupyter):

	```bash
	python -m ipykernel install --user --name sputnik-sr
	```

## Base de datos

- El archivo `data/schema.sql` define la estructura en SQLite (usuarios con roles, artistas, lanzamientos, interacciones, tracklists y vistas JSON).
- Para crear una base nueva o actualizar la estructura:

	```bash
	sqlite3 data/sputnik.db < data/schema.sql
	```

- Después de ingestas grandes conviene ejecutar `ANALYZE;` para mejorar el planner de SQLite.

## Scraper CLI

Para obtener el top anual directamente desde la línea de comandos (con rate limiting y reintentos incluidos):

```bash
python -m scraper --year 2024 --pretty > data/best_albums_2024.json
```

Si preferís integrarlo en un script, revisá `examples/fetch_latest.py`, que muestra cómo persistir los resultados.

## Crawler masivo

Ingiere artistas, discos, tracklists, soundoffs y perfiles públicos en SQLite:

```bash
python -m crawler \
    --start-year 1960 \
    --end-year 2025 \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --log-level DEBUG
```

Parámetros destacados:

- `--skip-tracklists` y `--skip-soundoffs`: omiten secciones específicas.
- `--max-soundoffs N`: limita soundoffs por álbum para hacer smoke tests.
- `--dry-run`: valida el scraping sin escribir en la base.
- `--log-level`: controla la verbosidad (`INFO` por defecto, `DEBUG` muestra cada soundoff/usuario procesado).
- `--skip-user-profiles`: no trae perfiles durante el crawl (igualmente encola usuarios si está habilitado).
- `--no-queue-users`: evita encolar usuarios detectados en soundoffs.
- `--user-queue-priority N`: prioridad para usuarios encolados desde soundoffs.

Características:

- Respeta rate limiting configurable (`min_interval`).
- Evita duplicados con `ON CONFLICT` y mantiene el último estado en `crawl_state` (status por año, último álbum, nota).
- Enriquece usuarios con su rol (ej. `EMERITUS`, `STAFF`) cuando aparece en los soundoffs o el perfil.

Para reanudar un crawl interrumpido alcanza con relanzar el comando; los años completados quedan marcados como `DONE` en `crawl_state`.

### Flujo escalonado de ingestión

Para poblar la base más rápido y profundizar después, podés separar la ingesta en tres etapas:

1. **Semillas (charts + soundoffs)**
   - Captura el top anual y, para cada álbum, ingiere las interacciones visibles en `soundoff.php`.
   - Encola automáticamente a los usuarios detectados en `crawl_users` si se habilita el encolado.
   - Podés ejecutar el comando directamente o usar el script `scripts/seed_charts.sh`:

        ```bash
        python -m crawler \
            --start-year 2000 \
            --end-year 2024 \
            --db data/sputnik.db \
            --schema data/schema.sql \
            --skip-tracklists \
            --skip-user-profiles \
            --user-queue-priority 5 \
            --log-level INFO
        ```

2. **Discografías extendidas**
   - Con los artistas detectados en la fase anterior, recorré `crawl_artists` para completar releases y metadatos antes de sumar más ratings.
   - El módulo `crawler.discography` trae la discografía pública y, de forma opcional, tracklists y soundoffs adicionales.
   - Recomendación: ejecutar este paso antes de expandir usuarios para que las interacciones futuras apunten a releases ya poblados.
   - Utilizar `scripts/expand_discographies.sh` o correr este ejemplo:

        ```bash
        python -m crawler.discography \
            --db data/sputnik.db \
            --schema data/schema.sql \
            --batch-size 25 \
            --max-soundoffs 100 \
            --log-level INFO
        ```

3. **Expansión de usuarios**
   - Consume `crawl_users` y trae los ratings públicos de cada perfil mediante `uservote.php` (no expone la fecha exacta del voto).
   - `crawler.user_expander` respeta claves únicas, actualiza perfiles y marca cada usuario como `done` o `error`.
   - Ejecutalo una vez que la discografía esté cargada para minimizar stubs y re-procesamientos.
   - Script sugerido: `scripts/expand_users.sh`

        ```bash
        python -m crawler.user_expander \
            --db data/sputnik.db \
            --schema data/schema.sql \
            --batch-size 25 \
            --max-rating-pages none \
            --log-level INFO
        ```

Los scripts aceptan los mismos flags que los módulos correspondientes y permiten parametrizar año de inicio/fin, prioridades, límites de páginas, etc. Asimismo respetan las variables de entorno `DB_PATH` y `SCHEMA_PATH` por si necesitás apuntar a otra base.

Recomendaciones:
- Usar batches pequeños para controlar rate limiting.
- Monitorear `crawl_users` / `crawl_artists` / `crawl_releases` y `crawl_state`.
- Relanzar los comandos sin miedo a duplicados (las tablas usan `ON CONFLICT`).

## Monitoreo y seguimiento

Hay un script interactivo que muestra el estado del crawler, colas e indicadores rápidos desde SQLite (requiere `sqlite3` en PATH):

```bash
scripts/monitor_crawler.sh data/sputnik.db logs/crawler-full.log
```

Funciones principales:
- Tail del log y procesos activos del crawler.
- Estado por año (`crawl_state`) y resumen por estado.
- Estadísticas de la base (releases, users, interactions, distribución de ratings, top releases por cantidad de votos).
- Estado de colas (`crawl_users`, `crawl_artists`, `crawl_releases`) y últimos errores.

## Chequeo de salud de la base de datos

Hay un chequeador automático para detectar y reparar problemas comunes en la base de datos (usuarios/releases con errores, metadata incompleta, ratings inconsistentes, etc).

**Diagnóstico:**

```bash
./scripts/db_health.sh
```

**Opciones útiles:**

- `--fix <categoria> --apply` repara automáticamente una categoría (ver sugerencias en la salida)
- `--fix-all --apply` intenta reparar todo lo posible
- `--format json` salida en JSON

Ejemplo para reparar usuarios con error de conexión:

```bash
./scripts/db_health.sh --fix users.error.connection --apply
```

El script muestra progreso, sugerencias y reporta el tiempo total. No borra datos válidos, solo reencola, resetea o elimina del queue según el caso.

Si preferís ejecutarlo directamente desde Python (útil para integrar en notebooks o pipelines CI):

```bash
python maintenance/db_health.py --db data/sputnik.db --format json
```

Se recomienda hacer VACUUM para desfragmentar la base de datos después de reparaciones o ingestas grandes, lo que ayuda a optimizar el espacio y el rendimiento.

```bash
sqlite3 data/sputnik.db "VACUUM;"
```

## Aplicación web de recomendaciones

Interfaz simple en Flask para probar recomendaciones con la base local.

- Módulo: `app/app.py`
- Puerto por defecto: `5050`
- Cookie usada: `id_usuario`

Pasos:

```bash
python -m app.app
```

Luego abrí http://localhost:5050, ingresá un usuario (id público de Sputnik) y vas a ver una lista de recomendaciones. La app:
- Marca como “visto” (rating = 0) cada candidato servido para evitar repeticiones.
- Permite calificar [0–5] y guarda en `interactions` (con `rating_date = now()`).
- Ofrece recomendaciones contextuales en `/recomendaciones/<id_release>` usando la tabla `release_recommendations`; si no hay suficientes candidatos, suma vecinos por co-ocurrencia (`release_pairs`), lanzamientos del mismo artista y, como último recurso, populares no vistos.

Para limpiar el historial del usuario activo: GET a `/reset`.

## Estructura del repositorio

- `scraper/`: parsing de HTML y cliente HTTP para Sputnik (`charts`, `soundoffs`, `tracklist`, `users`).
- `crawler/`: orquestadores que persisten en SQLite (`runner.py`, `discography.py`, `user_expander.py`).
- `app/`: app Flask y helpers de recomendaciones.
- `data/`: esquema SQL (`schema.sql`) y backups.
- `scripts/`: utilidades para sembrar, expandir y monitorear.
- `maintenance/`: scripts de mantenimiento y chequeo de salud (`db_health.py`).
- `offline_recommender/`: herramientas offline para recalcular co-ocurrencias, evaluar estrategias y almacenar resultados (`output/`).
- `tests/`: suite de pruebas de parsing e integración.
- `examples/`: uso del scraper desde scripts simples.
- `notebooks/`: EDA y exploración.

## Pruebas

```bash
pytest -q
```

- Filtrar por nombre con `-k` y modo detallado con `-vv`.

## Pre-commits

```bash
pre-commit install
pre-commit run --all-files
```

- Linter/formateo con Ruff configurado en `pyproject.toml`.

## Variables de entorno

- `SPUTNIK_DB`: ruta a la base SQLite utilizada por la app Flask. Si no se define, usa `data/sputnik.db`.
- Scripts bash aceptan: `DB_PATH`, `SCHEMA_PATH`, `START_YEAR_OVERRIDE`, `END_YEAR_OVERRIDE`, `BATCH_SIZE`, `MIN_INTERVAL`, `BURST_SIZE`, `MAX_SOUNDOFFS`, `LOG_LEVEL`.

## Notas sobre recomendaciones (heurística actual)

- Página principal: evalúa las señales del usuario y elige entre múltiples estrategias según el historial:
  - **Co-ocurrencia** (`release_pairs`) para usuarios con ≤8 calificaciones positivas.
  - **Recomendaciones avanzadas** para usuarios con ≥20 calificaciones positivas (requiere embeddings precomputados):
    - **Nivel 1 (20-29 calificaciones)**: Usa solo NMF
    - **Nivel 2 (≥30 calificaciones)**: Combina NMF + Two Towers con pesos y bonus de consenso
  - **Perfiles de contenido** (géneros y artistas) como fallback cuando las recomendaciones avanzadas no están disponibles.
  - **Popularidad** como fallback inicial.
  - Si aún quedan slots vacíos, mezcla candidatos populares y aleatorios para diversificar.
- Página de contexto: ensambla candidatos combinando `release_recommendations`, vecinos por `release_pairs`, otros lanzamientos del artista y populares no vistos.
- Los usuarios con ≥20 calificaciones positivas pueden generar o actualizar sus embeddings desde la interfaz web usando el botón unificado **"Recomendaciones avanzadas"**. El sistema detecta automáticamente el nivel y actualiza los sistemas correspondientes (NMF en nivel 1, NMF + Two Towers en nivel 2).
- Todas las interacciones se persisten en `interactions` con `rating` en [0, 5] y `rating_date = now()`.

## Sistema de recomendación híbrido

- Lógica central en `app/recommender.py` con configuración agrupada en la clase `Config`.
- Estrategias implementadas:
  - `recommend_from_pairs`: mezcla señales de co-ocurrencia ponderando rating y recencia.
  - `recommend_advanced`: sistema unificado que combina NMF y Two Towers según el nivel del usuario.
  - `recommend_content_based`: perfiles de usuario por géneros y artistas con prior de popularidad.
  - `recommend_nmf`: factorización matricial usando embeddings precomputados (NMF).
  - `recommend_two_towers`: aprendizaje profundo usando embeddings precomputados (Two Towers).
  - `recommend_random`: muestreo uniforme de lanzamientos no vistos para exploración controlada.
- Lógica híbrida en `recommend`:
  - Usuarios sin ratings → populares + aleatorios.
  - Hasta 8 ratings positivos → co-ocurrencia.
  - 9 ratings positivos → contenido.
  - 20+ ratings positivos → recomendaciones avanzadas (NMF en nivel 1, NMF + Two Towers en nivel 2).
  - Diversificación por artista para evitar repeticiones.
- `recommend_context` sigue la misma filosofía: recomendaciones directas, pares, artista y fallback popular.
- La app muestra explicaciones en la UI y expone `last_strategy` / `last_context_strategy` para instrumentación.
- Los usuarios con ≥20 calificaciones positivas pueden actualizar sus embeddings desde la interfaz web usando el botón unificado "Recomendaciones avanzadas".

### Rebuilding de co-ocurrencias

- Tabla `release_pairs` con métricas `pair_count`, `jaccard`, `lift` y `last_built_at`.
- Script `offline_recommender/build_release_pairs.py` recalcula las co-ocurrencias:

    ```bash
    python offline_recommender/build_release_pairs.py \
        --database data/sputnik.db \
        --min-rating 3.0 \
        --min-pair-count 3
    ```

- Ajusta el umbral de rating y el mínimo de pares según el tamaño de la base.

### Construcción de embeddings para Recomendaciones Avanzadas

El sistema de recomendaciones avanzadas requiere embeddings de NMF y Two Towers. Los usuarios pueden actualizar sus embeddings desde la interfaz web usando el botón unificado **"Recomendaciones avanzadas"**, que detecta automáticamente el nivel y actualiza los sistemas correspondientes.

#### Embeddings NMF (Nivel 1)

- Tablas `user_embeddings` y `release_embeddings` almacenan vectores de factores latentes.
- Script `offline_recommender/build_nmf_embeddings.py` construye embeddings de releases:

    ```bash
    python offline_recommender/build_nmf_embeddings.py \
        --database data/sputnik.db \
        --n-components 50 \
        --min-user-ratings 10 \
        --min-release-ratings 5
    ```

#### Embeddings Two Towers (Nivel 2)

- Tablas `user_embeddings_dl` y `release_embeddings_dl` almacenan vectores de aprendizaje profundo.
- Script `offline_recommender/build_two_towers.py` entrena el modelo y construye embeddings:

    ```bash
    python offline_recommender/build_two_towers.py \
        --database data/sputnik.db \
        --embedding-dim 64 \
        --epochs 10 \
        --batch-size 1024 \
        --min-user-ratings 5 \
        --min-release-ratings 3
    ```

**Nota**: Los embeddings de releases deben reconstruirse periódicamente cuando haya nuevas interacciones en el sistema (típicamente semanal). Los usuarios pueden actualizar sus embeddings individuales en cualquier momento desde la interfaz web.

### Configuración rápida de hiperparámetros

- La clase `Config` permite modificar sin tocar la lógica:
  - `positive_rating_threshold`: rating mínimo para considerar "señal positiva" (default: 3.0).
  - `max_pairs_signals`: hasta cuántos ratings positivos disparan la estrategia de pares (default: 8).
  - `min_advanced_level_1_signals`: mínimo de ratings positivos para activar recomendaciones avanzadas nivel 1 (default: 20).
  - `min_advanced_level_2_signals`: mínimo de ratings positivos para activar recomendaciones avanzadas nivel 2 (default: 30).
  - `advanced_nmf_weight`: peso de NMF en combinación nivel 2 (default: 0.6).
  - `advanced_two_towers_weight`: peso de Two Towers en combinación nivel 2 (default: 0.4).
  - `advanced_consensus_bonus`: bonus para candidatos que aparecen en ambos sistemas (default: 0.2).
  - `genre_weight` / `artist_weight` / `popularity_prior`: balance de cada componente en el score content-based.
  - `recency_log_base`: logaritmo usado para atenuar la recencia.
  - `candidate_pool_multiplier`: expande la búsqueda de candidatos en contenido.
  - `pairs_limit_multiplier` / `pairs_table_sample`: controlan el tamaño del sample en co-ocurrencia.
  - `popularity_recent_divisor`: suaviza el bonus por discos recientes.

Modificá estos valores al inicio del módulo para experimentar sin reescribir funciones.

## Evaluación offline

- Script `offline_recommender/evaluate_recommender.py` calcula NDCG@k por estrategia sobre usuarios con suficientes ratings.
- Ejecutalo especificando la DB y la cantidad de usuarios a muestrear:

    ```bash
    python offline_recommender/evaluate_recommender.py \
        --database data/sputnik.db \
        --min-ratings 50 \
        --sample-size 100 \
        --k 9 \
        --output offline_recommender/output/results.csv \
        --verbose
    ```

- **Salida**
  - Promedio de NDCG@k por estrategia (híbrido, recomendaciones avanzadas, pares, contenido, aleatorio, popularidad).
  - Con `--verbose`, loggea por consola el NDCG de cada usuario evaluado.
  - El CSV indicado en `--output` contiene una fila por usuario y puede analizarse luego en pandas o planillas; se recomienda guardarlo en `offline_recommender/output/`.
- Reporta métricas promedio para:
  - Híbrido (`recommend`)
  - Recomendaciones avanzadas (`recommend_advanced`)
  - Co-ocurrencia (`recommend_from_pairs`)
  - NMF (`recommend_nmf`)
  - Two Towers (`recommend_two_towers`)
  - Contenido (`recommend_content_based`)
  - Aleatorio (`recommend_random`)
  - Popularidad (`_popular_unseen_releases`)
- El CSV opcional permite seguir la evolución usuario por usuario.
