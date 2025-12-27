# Extracción de Datos

Este documento describe en detalle el pipeline de scraping y crawling para extraer datos de Sputnikmusic.

## Tabla de Contenidos

1. [Arquitectura General](#arquitectura-general)
2. [Scraper CLI](#scraper-cli)
3. [Crawler Masivo](#crawler-masivo)
4. [Flujo Escalonado de Ingestión](#flujo-escalonado-de-ingestión)
5. [Monitoreo y Seguimiento](#monitoreo-y-seguimiento)
6. [Mantenimiento de la Base de Datos](#mantenimiento-de-la-base-de-datos)
7. [Esquema de Datos](#esquema-de-datos)
8. [Consideraciones Éticas](#consideraciones-éticas)

---

## Arquitectura General

El sistema de extracción se compone de dos capas principales:

### Capa de Scraping (`scraper/`)

Módulos de bajo nivel para parsing de HTML y comunicación HTTP:

| Módulo | Descripción |
|--------|-------------|
| `charts.py` | Extrae rankings anuales de álbumes |
| `soundoffs.py` | Parsea comentarios y ratings de usuarios en releases |
| `tracklist.py` | Extrae listados de canciones |
| `users.py` | Obtiene perfiles públicos de usuarios |
| `user_ratings.py` | Extrae historial de calificaciones de usuarios |
| `discography.py` | Parsea discografías completas de artistas |
| `http.py` | Cliente HTTP con rate limiting y reintentos |

### Capa de Crawling (`crawler/`)

Orquestadores de alto nivel que coordinan el scraping y persisten en SQLite:

| Módulo | Descripción |
|--------|-------------|
| `runner.py` | Crawler principal: charts → releases → soundoffs |
| `discography.py` | Expande discografías de artistas encolados |
| `user_expander.py` | Trae ratings completos de usuarios encolados |

---

## Scraper CLI

Para obtener datos puntuales sin persistir en base de datos:

```bash
# Top anual en JSON
python -m scraper --year 2024 --pretty > data/best_albums_2024.json

# Con más detalle
python -m scraper --year 2024 --pretty --verbose
```

### Opciones del Scraper

| Flag | Descripción |
|------|-------------|
| `--year` | Año del chart a extraer |
| `--pretty` | Formato JSON indentado |
| `--verbose` | Logging detallado |

### Ejemplo de Integración

Ver `examples/fetch_latest.py` para un ejemplo de cómo usar el scraper programáticamente:

```python
from scraper import charts

# Obtener chart del año
albums = charts.fetch_year_chart(2024)

for album in albums:
    print(f"{album['artist']} - {album['title']} ({album['rating']})")
```

---

## Crawler Masivo

El crawler principal ingiere datos de forma sistemática y los persiste en SQLite.

### Comando Básico

```bash
python -m crawler \
    --start-year 1960 \
    --end-year 2025 \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --log-level INFO
```

### Parámetros Principales

| Parámetro | Descripción | Default |
|-----------|-------------|---------|
| `--start-year` | Año inicial del crawl | - |
| `--end-year` | Año final del crawl | - |
| `--db` | Ruta a la base SQLite | `data/sputnik.db` |
| `--schema` | Ruta al esquema SQL | `data/schema.sql` |
| `--log-level` | Nivel de logging | `INFO` |

### Parámetros de Control

| Parámetro | Descripción |
|-----------|-------------|
| `--skip-tracklists` | Omite extracción de tracklists |
| `--skip-soundoffs` | Omite extracción de soundoffs |
| `--skip-user-profiles` | No trae perfiles durante el crawl |
| `--max-soundoffs N` | Limita soundoffs por álbum |
| `--dry-run` | Valida sin escribir en la base |
| `--no-queue-users` | No encola usuarios detectados |
| `--user-queue-priority N` | Prioridad para usuarios encolados |

### Características del Crawler

- **Rate limiting configurable**: Respeta `min_interval` entre requests
- **Idempotente**: Usa `ON CONFLICT` para evitar duplicados
- **Reanudable**: Mantiene estado en `crawl_state` (status por año)
- **Enriquecimiento**: Detecta roles de usuarios (EMERITUS, STAFF, etc.)

### Reanudación

Para reanudar un crawl interrumpido, simplemente relanzar el comando:

```bash
# Los años completados quedan marcados como DONE en crawl_state
python -m crawler --start-year 1960 --end-year 2025 --db data/sputnik.db
```

---

## Flujo Escalonado de Ingestión

Para poblar la base de forma eficiente, se recomienda separar la ingesta en tres etapas:

### Etapa 1: Semillas (Charts + Soundoffs)

Captura los rankings anuales y las interacciones visibles en cada álbum.

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

**Script alternativo:** `scripts/seed_charts.sh`

**Resultado:**
- Artistas y releases del top anual
- Interacciones de soundoffs
- Usuarios encolados en `crawl_users`
- Artistas encolados en `crawl_artists`

### Etapa 2: Discografías Extendidas

Expande la discografía completa de cada artista detectado.

```bash
python -m crawler.discography \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --batch-size 25 \
    --max-soundoffs 100 \
    --log-level INFO
```

**Script alternativo:** `scripts/expand_discographies.sh`

**Parámetros:**

| Parámetro | Descripción | Default |
|-----------|-------------|---------|
| `--batch-size` | Artistas por batch | 25 |
| `--max-soundoffs` | Soundoffs por release | 100 |
| `--skip-tracklists` | Omite tracklists | false |
| `--skip-soundoffs` | Omite soundoffs | false |

**Recomendación:** Ejecutar antes de expandir usuarios para que las interacciones futuras apunten a releases ya poblados.

### Etapa 3: Expansión de Usuarios

Trae el historial completo de ratings de cada usuario encolado.

```bash
python -m crawler.user_expander \
    --db data/sputnik.db \
    --schema data/schema.sql \
    --batch-size 25 \
    --max-rating-pages none \
    --log-level INFO
```

**Script alternativo:** `scripts/expand_users.sh`

**Parámetros:**

| Parámetro | Descripción | Default |
|-----------|-------------|---------|
| `--batch-size` | Usuarios por batch | 25 |
| `--max-rating-pages` | Páginas de ratings por usuario | `none` (todas) |
| `--priority-min` | Prioridad mínima para procesar | 0 |

**Nota:** El endpoint `uservote.php` no expone la fecha exacta del voto, solo el rating.

### Recomendaciones Generales

- **Batches pequeños**: Facilita el control de rate limiting
- **Monitorear colas**: Revisar `crawl_users`, `crawl_artists`, `crawl_releases`
- **Relanzar sin miedo**: Las tablas usan `ON CONFLICT` para evitar duplicados
- **Variables de entorno**: Los scripts respetan `DB_PATH`, `SCHEMA_PATH`, etc.

---

## Monitoreo y Seguimiento

### Script de Monitoreo Interactivo

```bash
scripts/monitor_crawler.sh data/sputnik.db logs/crawler-full.log
```

**Funciones:**
- Tail del log en tiempo real
- Procesos activos del crawler
- Estado por año (`crawl_state`)
- Estadísticas de la base (releases, users, interactions)
- Distribución de ratings
- Top releases por cantidad de votos
- Estado de colas y últimos errores

### Consultas Útiles

```sql
-- Estado del crawl por año
SELECT year, status, last_album, note FROM crawl_state ORDER BY year;

-- Usuarios pendientes
SELECT COUNT(*) FROM crawl_users WHERE status = 'pending';

-- Artistas con errores
SELECT id_artist, last_error FROM crawl_artists WHERE status = 'error' LIMIT 10;

-- Distribución de ratings
SELECT ROUND(rating, 1) as rating, COUNT(*) as count
FROM interactions
WHERE rating > 0
GROUP BY ROUND(rating, 1)
ORDER BY rating;
```

---

## Mantenimiento de la Base de Datos

### Chequeo de Salud

Detecta y repara problemas comunes:

```bash
# Diagnóstico
./scripts/db_health.sh

# Ver en JSON
python maintenance/db_health.py --db data/sputnik.db --format json

# Reparar categoría específica
./scripts/db_health.sh --fix users.error.timeout --apply

# Reparar todo
./scripts/db_health.sh --fix-all --apply
```

**Categorías de problemas detectados:**

| Categoría | Descripción |
|-----------|-------------|
| `users.error.*` | Usuarios con errores (404, timeout, conexión) |
| `users.incomplete` | Perfiles sin role o join_date |
| `users.ratings_mismatch` | ratings_count inconsistente |
| `releases.error.*` | Releases con errores |
| `releases.incomplete` | Metadata incompleta |
| `artists.no_genres` | Artistas sin géneros asignados |

### Optimización

Después de ingestas grandes o reparaciones:

```bash
# VACUUM para desfragmentar
sqlite3 data/sputnik.db "VACUUM;"

# ANALYZE para actualizar estadísticas del planner
sqlite3 data/sputnik.db "ANALYZE;"

# Script completo de análisis y optimización
python maintenance/analyze_and_vacuum.py
```

📖 Ver [maintenance/README.md](../maintenance/README.md) para más detalles.

---

## Esquema de Datos

### Tablas Principales

```
users              # Perfiles de usuarios
artists            # Artistas
releases           # Álbumes, EPs, Singles, Compilations
interactions       # Ratings y soundoffs de usuarios
```

### Tablas de Crawling

```
crawl_state        # Estado del crawl por año
crawl_users        # Cola de usuarios a expandir
crawl_artists      # Cola de artistas a expandir
crawl_releases     # Cola de releases a procesar
```

### Tablas Auxiliares

```
genres             # Catálogo de géneros
artist_genres      # Géneros por artista
release_genres     # Géneros por release
release_tracks     # Tracklists
artist_similars    # Artistas similares
release_recommendations  # Recomendaciones pre-calculadas
release_pairs      # Co-ocurrencias para recomendación
```

### Tablas de Embeddings

```
user_embeddings       # Embeddings NMF de usuarios
release_embeddings    # Embeddings NMF de releases
user_embeddings_dl    # Embeddings Two Towers de usuarios
release_embeddings_dl # Embeddings Two Towers de releases
```

### Vistas JSON

```
artists_enriched   # Artistas con géneros y similares en JSON
releases_enriched  # Releases con recomendaciones y tracklist en JSON
```

### Diagrama Simplificado

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   artists   │────<│  releases   │────<│interactions │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                   │
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│artist_genres│     │release_genres│     │    users    │
└─────────────┘     └──────────────┘     └─────────────┘
```

---

## Consideraciones Éticas

### Rate Limiting

El sistema implementa rate limiting para no sobrecargar Sputnikmusic:

- **Intervalo mínimo**: Configurable vía `min_interval`
- **Burst control**: Límite de requests consecutivos
- **Backoff exponencial**: En caso de errores o rate limiting

### Respeto a robots.txt

El scraper respeta las directivas del sitio y evita endpoints protegidos.

### Uso de Datos

- Los datos son solo para análisis personal
- No se redistribuyen datasets
- El proyecto es exclusivamente educativo

### Buenas Prácticas

1. **Usar batches pequeños**: Reduce la carga en el servidor
2. **Monitorear errores**: Detectar y respetar rate limiting
3. **Horarios de bajo tráfico**: Preferir crawling en horarios nocturnos
4. **Cachear resultados**: Evitar re-crawlear datos ya obtenidos

---

## Troubleshooting

### Error: "database is locked"

SQLite no soporta escrituras concurrentes. Soluciones:
- Usar WAL mode: `PRAGMA journal_mode=WAL;`
- Reducir concurrencia en el crawler
- Esperar y reintentar automáticamente

### Error: Rate limiting (429)

El servidor está limitando requests:
- Aumentar `min_interval`
- Reducir `batch_size`
- Esperar antes de reintentar

### Usuarios/Releases con status 'error'

Usar el script de salud para diagnosticar:

```bash
./scripts/db_health.sh --format json | jq '.users.errors'
```

Reparar según el tipo de error:
- **404**: Usuario/release eliminado → eliminar de cola
- **timeout**: Problema temporal → reencolar
- **connection**: Problema de red → reintentar

### Datos incompletos

Verificar con:

```sql
-- Releases sin año
SELECT COUNT(*) FROM releases WHERE release_year IS NULL;

-- Usuarios sin role
SELECT COUNT(*) FROM users WHERE role IS NULL;
```

Reencolar para completar:

```bash
./scripts/db_health.sh --fix releases.incomplete --apply
```

---

## Referencias

- **Módulos de scraping**: `scraper/`
- **Módulos de crawling**: `crawler/`
- **Scripts de utilidad**: `scripts/`
- **Mantenimiento**: `maintenance/`
- **Esquema SQL**: `data/schema.sql`
