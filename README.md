# sputnik-SR

[Español](#español) | [English](#english)

---

## Español

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

---

## English

Album recommendation system built from [Sputnikmusic](https://www.sputnikmusic.com/) data.

## Purpose

**This project is educational only.** Built to learn web scraping, data processing, and recommender systems. It implements rate limiting, delays between requests, and ethical scraping practices to avoid overloading the platform.

## Overview

The project is split into two main stages:

### Stage 1: Data Extraction

Scraping + crawling pipeline that collects from Sputnikmusic:
- **Artists and complete discographies**
- **Releases** and metadata
- **User interactions**
- **User profiles** (roles and statistics)

[Detailed extraction docs (EN)](docs/data-extraction.md)

### Stage 2: Recommendation System

**RRF-Ensemble**: rank fusion via Reciprocal Rank Fusion

Hybrid engine that combines multiple strategies to produce personalized recommendations:
- **NMF**: Non-negative Matrix Factorization
- **Two Towers**: Deep-learning architecture
- **Co-occurrence**: consumption co-occurrence signals
- **Content-based**: genre + artist profiles

[Detailed strategy docs (EN)](docs/recommendation-strategies.md)

### Offline evaluation and main findings

[Results notebook](notebooks/analisis_evaluacion_recomendaciones.ipynb)

Note: the results notebooks are written in Spanish, but they should be easy to interpret via the plots, tables, and code.

## Quickstart

### Installation

```bash
# Create environment
mamba env create -f environment.yml
conda activate sputnik-sr

# Initialize database
sqlite3 data/sputnik.db < data/schema.sql
```

### Data extraction

```bash
# Fetch yearly charts with soundoffs
python -m crawler --start-year 1960 --end-year 2025 --db data/sputnik.db

# Expand artist discographies
python -m crawler.discography --db data/sputnik.db --batch-size 25

# Expand user ratings
python -m crawler.user_expander --db data/sputnik.db --batch-size 25
```

### Recommendation system

```bash
# Build co-occurrences
python offline_recommender/build_release_pairs.py --database data/sputnik.db

# Build NMF embeddings
python offline_recommender/build_nmf_embeddings.py --database data/sputnik.db

# Build Two Towers embeddings
python offline_recommender/build_two_towers.py --database data/sputnik.db

# Start web app
python -m app.app
# Open http://localhost:5050
```

## Repository structure

```
sputnik-SR/
├── scraper/              # HTML parsing and HTTP client
├── crawler/              # Crawling orchestrators
├── app/                  # Flask app and recommender engine
├── offline_recommender/  # Build + evaluation scripts
├── maintenance/          # DB health + optimization scripts
├── data/                 # SQL schema and databases
├── models/               # Trained models and vocabularies
├── scripts/              # Bash utilities
├── tests/                # Test suite
├── notebooks/            # EDA + evaluation notebooks
└── docs/                 # Detailed documentation
```

## Documentation

| Document | Description |
|----------|-------------|
| [Data Extraction](docs/data-extraction.md) | Scraping, crawling, ingestion flow, monitoring |
| [Recommendation Strategies](docs/recommendation-strategies.md) | Algorithms, metrics, configuration, evaluation |
| [Maintenance](maintenance/README.en.md) | DB health and optimization scripts |

## Development

```bash
# Run tests
pytest -q

# Linter
ruff check .

# Pre-commits
pre-commit install
pre-commit run --all-files
```

## License

MIT License - see [LICENSE](LICENSE) for details.
