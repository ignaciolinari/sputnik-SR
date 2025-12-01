# sputnik-SR

Sistema de recomendación de discos construido a partir de datos de [Sputnikmusic](https://www.sputnikmusic.com/).

## Propósito

**Este proyecto es exclusivamente educativo.** Desarrollado para aprender sobre web scraping, procesamiento de datos y sistemas de recomendación. Implementa rate limiting, delays entre requests y scraping ético para no sobrecargar la plataforma.

## Visión General

El proyecto se divide en dos etapas principales:

### Etapa 1: Extracción de Datos

Pipeline de scraping y crawling que recolecta información de Sputnikmusic:
- **Artistas y discografías** completas
- **Releases** con metadata
- **Interacciones** de usuarios
- **Perfiles de usuarios** con roles y estadísticas

[Documentación detallada de extracción](docs/extraccion-datos.md)

### Etapa 2: Sistema de Recomendación

**RRF-Ensemble**: Fusión de rankings con Reciprocal Rank Fusion

Motor híbrido que combina múltiples estrategias para generar recomendaciones personalizadas formado por:
- **NMF**: Factorización matricial no negativa
- **Two Towers**: Arquitectura de deep learning
- **Co-ocurrencia**: Basado en patrones de consumo conjunto
- **Content-based**: Perfiles de géneros y artistas

[Documentación detallada de estrategias](docs/estrategias-recomendacion.md)

### Evaluación offline y hallazgos principales

[Notebook de Resultados](notebooks/analisis_evaluacion_recomendaciones.ipynb)

## Inicio Rápido

### Instalación

```bash
# Crear entorno
mamba env create -f environment.yml
conda activate sputnik-sr

# Inicializar base de datos
sqlite3 data/sputnik.db < data/schema.sql
```

### Extracción de datos

```bash
# Obtener charts anuales con soundoffs
python -m crawler --start-year 1960 --end-year 2025 --db data/sputnik.db

# Expandir discografías de artistas
python -m crawler.discography --db data/sputnik.db --batch-size 25

# Expandir ratings de usuarios
python -m crawler.user_expander --db data/sputnik.db --batch-size 25
```

### Sistema de recomendación

```bash
# Construir co-ocurrencias
python offline_recommender/build_release_pairs.py --database data/sputnik.db

# Construir embeddings NMF
python offline_recommender/build_nmf_embeddings.py --database data/sputnik.db

# Construir embeddings Two Towers
python offline_recommender/build_two_towers.py --database data/sputnik.db

# Iniciar aplicación web
python -m app.app
# Abrir http://localhost:5050
```

## Estructura del Repositorio

```
sputnik-SR/
├── scraper/              # Parsing de HTML y cliente HTTP
├── crawler/              # Orquestadores de crawling
├── app/                  # Aplicación Flask y motor de recomendación
├── offline_recommender/  # Scripts de construcción y evaluación
├── maintenance/          # Scripts de mantenimiento de la DB
├── data/                 # Esquema SQL y bases de datos
├── models/               # Modelos entrenados y vocabularios
├── scripts/              # Utilidades bash
├── tests/                # Suite de pruebas
├── notebooks/            # Análisis exploratorios y de resultados finales
└── docs/                 # Documentación detallada
```

## Documentación

| Documento | Descripción |
|-----------|-------------|
| [Extracción de Datos](docs/extraccion-datos.md) | Scraping, crawling, flujo de ingestión, monitoreo |
| [Estrategias de Recomendación](docs/estrategias-recomendacion.md) | Algoritmos, métricas, configuración, evaluación |
| [Mantenimiento](maintenance/README.md) | Scripts de salud y optimización de la DB |

## Desarrollo

```bash
# Ejecutar tests
pytest -q

# Linter
ruff check .

# Pre-commits
pre-commit install
pre-commit run --all-files
```

## Licencia

MIT License - Ver [LICENSE](LICENSE) para más detalles.
