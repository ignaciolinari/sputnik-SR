# sputnik-SR

Proyecto para construir un sistema de recomendación de discos a partir de datos obtenidos en Sputnikmusic.

## Tabla de contenidos
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

## Monitoreo y seguimiento

- Consultá el progreso persistido:

	```bash
	sqlite3 data/sputnik.db "SELECT year, status, last_note, updated_at FROM crawl_state ORDER BY year" | tail
	```

- El script `scripts/monitor_crawler.sh` muestra un panel con proceso, tail del log y contadores básicos. Ejemplo:

	```bash
	bash scripts/monitor_crawler.sh --db data/sputnik.db --log logs/crawler-full.log
	```

- Los logs detallados quedan en `logs/crawler-full.log` (o el archivo que definas). Para ejecuciones largas se recomienda redirigir la salida estándar a un log con `> archivo.log 2>&1 &`.

## Estructura del repositorio

- `app/`: aplicación web (placeholder).
- `data/`: esquema SQL, base de datos y backups locales.
- `examples/`: scripts de demostración del cliente.
- `scraper/`: cliente HTTP, parsers de charts/soundoffs/tracklists/usuarios.
- `crawler/`: orquestador de ingesta hacia SQLite.
- `scripts/`: utilidades de monitoreo.
- `tests/`: pruebas unitarias (`pytest`).
- `environment.yml`: definición de entorno.
- `pyproject.toml`: configuración de Ruff y pytest.

## Pruebas

```bash
pytest
```

La batería valida los parsers (charts, soundoffs, usuarios), la lógica del cliente HTTP (reintentos, rate limiting) y los loaders auxiliares.

## Pre-commits

1. Instalar los hooks (con el entorno activo):

	```bash
	pre-commit install
	```

2. Ejecutar sobre todo el repositorio cuando quieras verificar el estado actual:

	```bash
	pre-commit run --all-files
	```

Los hooks ejecutan `ruff`, `black`, `isort` y validaciones básicas de formato.
