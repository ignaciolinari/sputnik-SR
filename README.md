# sputnik-SR

Proyecto para construir un sistema de recomendación de discos a partir de datos obtenidos en Sputnikmusic.

## Tabla de contenidos
- [Entorno de desarrollo](#entorno-de-desarrollo)
- [Esquema de base de datos](#esquema-de-base-de-datos)
- [Pre-commits](#pre-commits)
- [Estructura del repositorio](#estructura-del-repositorio)

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

## Esquema de base de datos

El archivo `data/schema.sql` define la estructura en SQLite (usuarios, artistas, lanzamientos, interacciones y tablas auxiliares para features content-based).

Para crear una base desde cero:

```bash
sqlite3 data/sputnik.db < data/schema.sql
```

- El script habilita llaves foráneas y vistas JSON (`artists_enriched`, `releases_enriched`).
- `data/` queda como directorio de trabajo local; los archivos `.db` están ignorados por Git.
- Ejecuta `ANALYZE;` después de ingestas grandes para mejorar el planner de SQLite.

## Pre-commits

1. Instalar los hooks (con el entorno activo):

	```bash
	pre-commit install
	```

2. Opcionalmente, correrlos sobre todo el repo para validar el estado actual:

	```bash
	pre-commit run --all-files
	```

Los hooks aplican formato (`black`, `ruff-format`, `isort`) y verificaciones básicas (YAML, espacios finales, archivos grandes).

## Estructura del repositorio
