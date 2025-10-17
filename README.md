# sputnik-SR

Proyecto para construir un sistema de recomendación de discos a partir de datos obtenidos en Sputnikmusic.

## Propósito y Responsabilidad

**Este proyecto es exclusivamente educativo.**

- **Fin académico**: Desarrollado para aprender sobre web scraping, procesamiento de datos y sistemas de recomendación.
- **Respeto a la plataforma**: Implementa rate limiting, delays entre requests y scraping ético para no sobrecargar Sputnikmusic.
- **Uso responsable**: Los datos obtenidos son solo para análisis personal y no se redistribuyen.
- **Cumplimiento**: Respeta los términos de servicio del sitio y evita cualquier impacto negativo en su funcionamiento.

## Tabla de contenidos
- [Propósito y Responsabilidad](#️-propósito-y-responsabilidad)
- [Entorno de desarrollo](#entorno-de-desarrollo)
- [Base de datos](#base-de-datos)
- [Scraper CLI](#scraper-cli)
- [Crawler masivo](#crawler-masivo)
- [Monitoreo y seguimiento](#monitoreo-y-seguimiento)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Pruebas](#pruebas)
- [Pre-commits](#pre-commits)

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

> Recomendaciones: usar batches pequeños para controlar rate limiting, monitorear `crawl_users`/`crawl_releases`, y relanzar los comandos sin miedo a duplicados (las tablas usan `ON CONFLICT`
