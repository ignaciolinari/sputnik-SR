# Informe Comparativo: Scripts de Mantenimiento de Base de Datos

## Resumen Ejecutivo

Este proyecto tiene dos scripts de mantenimiento con propósitos diferentes:

1. **`db_health.py`** - Para verificar problemas durante el proceso de crawling/scraping
2. **`analyze_and_vacuum.py`** - Para mantenimiento post-población (análisis y optimización)

---

## 1. `maintenance/db_health.py`

### Propósito
Herramienta para auditar la salud de la base de datos **durante el proceso de crawling/scraping**. Detecta problemas relacionados con el proceso de recolección de datos.

### Funcionalidades Principales

#### Checks que Realiza:
1. **Errores en cola de usuarios** (`crawl_users`)
   - Detecta usuarios con status 'error'
   - Clasifica errores: 404, rate limiting, timeouts, conexión, etc.
   - Puede eliminar o resetear usuarios según el tipo de error

2. **Perfiles de usuario incompletos**
   - Usuarios con `role` o `join_date` NULL
   - Sugiere reencolar para completar datos

3. **Mismatches de ratings de usuarios**
   - Compara `users.ratings_count` vs conteo real en `interactions`
   - Detecta ratings faltantes

4. **Errores en cola de releases** (`crawl_releases`)
   - Similar a usuarios, detecta releases con errores
   - Clasifica y sugiere acciones

5. **Metadata incompleta de releases**
   - Releases sin `release_year` o con ratings inconsistentes
   - Sugiere reencolar para completar

6. **Mismatches de ratings de releases**
   - Compara `releases.ratings_count` vs conteo real en `interactions`

7. **Artistas sin géneros**
   - Artistas marcados como 'done' pero sin géneros asignados

### Características Clave:
- **Reparación automática**: Puede aplicar fixes con `--fix` y `--apply`
- **Clasificación de severidad**: critical, high, medium, low
- **Muestras de problemas**: Muestra ejemplos de cada tipo de issue
- **Dry-run por defecto**: No aplica cambios a menos que uses `--apply`
- **Formato JSON**: Soporta `--format json` para integración

### Cuándo Usar:
- **Durante el crawling**: Para monitorear y resolver problemas del proceso de scraping
- **Después de errores masivos**: Para identificar y reparar problemas después de fallos
- **Mantenimiento de colas**: Para limpiar o resetear entradas en colas de crawling

### Ejemplo de Uso:
```bash
# Ver problemas
python maintenance/db_health.py --db data/sputnik.db

# Ver en JSON
python maintenance/db_health.py --db data/sputnik.db --format json

# Reparar errores temporales (dry-run)
python maintenance/db_health.py --db data/sputnik.db --fix users.error.timeout

# Aplicar reparaciones
python maintenance/db_health.py --db data/sputnik.db --fix users.error.timeout --apply

# Reparar todo automáticamente
python maintenance/db_health.py --db data/sputnik.db --fix-all --apply
```

### Rendimiento:
- **Puede ser lento** en bases grandes debido a queries complejas con JOINs y GROUP BY
- **Escanea tablas grandes** como `interactions` para detectar mismatches

---

## 2. `maintenance/analyze_and_vacuum.py`

### Propósito
Script para **mantenimiento post-población**: análisis de estadísticas generales y optimización mediante VACUUM.

### Funcionalidades Principales

#### Análisis que Realiza:
1. **Estadísticas de tamaño**
   - Tamaño del archivo en disco
   - Páginas totales y tamaño de página
   - Páginas libres (espacio desperdiciado)

2. **Verificación de integridad**
   - `PRAGMA quick_check` (rápido por defecto)
   - `PRAGMA integrity_check` (completo si se solicita)

3. **Lista de tablas**
   - Enumera todas las tablas
   - Opcionalmente cuenta filas (puede ser lento)

4. **Health checks opcionales**
   - Puede ejecutar `db_health.py` si se solicita con `--include-health`
   - No recomendado para uso regular (muy lento)

#### Vacuum:
- Ejecuta `VACUUM` para desfragmentar y optimizar la base de datos
- Muestra reducción de tamaño antes/después
- Estadísticas post-vacuum

### Características Clave:
- **Rápido por defecto**: Usa `quick_check` y evita operaciones costosas
- **Modo rápido**: No cuenta filas ni ejecuta health checks por defecto
- **Optimización**: Ejecuta VACUUM para desfragmentar
- **Flexible**: Opciones para análisis completo si se necesita

### Cuándo Usar:
- **Mantenimiento regular**: Para verificar integridad y optimizar espacio
- **Después de operaciones grandes**: Después de DELETE masivos o cambios estructurales
- **Monitoreo de tamaño**: Para verificar crecimiento y espacio desperdiciado
- **Antes de backups**: Para asegurar que la base está optimizada

### Ejemplo de Uso:
```bash
# Análisis rápido + vacuum (recomendado)
python maintenance/analyze_and_vacuum.py

# Solo vacuum (más rápido)
python maintenance/analyze_and_vacuum.py --vacuum-only

# Solo análisis
python maintenance/analyze_and_vacuum.py --analyze-only

# Análisis completo (incluye counts - lento)
python maintenance/analyze_and_vacuum.py --include-counts

# Solo una base de datos
python maintenance/analyze_and_vacuum.py --lite-only
python maintenance/analyze_and_vacuum.py --full-only
```

### Rendimiento:
- **Rápido por defecto**: Solo operaciones básicas de SQLite
- **VACUUM puede tardar**: En bases grandes (8GB+) puede tomar varios minutos
- **Con counts es lento**: `--include-counts` hace COUNT(*) en todas las tablas

---

## Comparación Directa

| Aspecto | `db_health.py` | `analyze_and_vacuum.py` |
|---------|----------------|-------------------------|
| **Propósito** | Verificar problemas de crawling | Mantenimiento post-población |
| **Enfoque** | Problemas de datos y colas | Estadísticas e integridad |
| **Velocidad** | Lento (queries complejas) | Rápido (por defecto) |
| **Reparación** | Sí, con `--fix --apply` | No, solo análisis |
| **VACUUM** | No | Sí |
| **Integridad** | No | Sí (quick_check) |
| **Uso típico** | Durante scraping | Mantenimiento regular |

---

## Flujo de Trabajo Recomendado

### Durante el Crawling/Scraping:
```bash
# Monitorear problemas del proceso
python maintenance/db_health.py --db data/sputnik.db

# Reparar problemas detectados
python maintenance/db_health.py --db data/sputnik.db --fix-all --apply
```

### Mantenimiento Post-Población:
```bash
# Análisis rápido + optimización
python maintenance/analyze_and_vacuum.py

# Si hay problemas de integridad, análisis completo
python maintenance/analyze_and_vacuum.py --full-analysis
```

### Mantenimiento Regular (Mensual):
```bash
# Verificar ambas bases
python maintenance/analyze_and_vacuum.py

# Si hay espacio desperdiciado, el vacuum lo optimizará
```

---

## Notas Importantes

1. **`db_health.py` es específico del proceso de crawling**: Sus checks asumen que hay tablas `crawl_users`, `crawl_releases`, etc. No tiene sentido usarlo en bases que no están siendo pobladas activamente.

2. **`analyze_and_vacuum.py` es genérico**: Funciona con cualquier base SQLite y es útil para mantenimiento general.

3. **VACUUM bloquea la base**: Durante el VACUUM, la base queda bloqueada para escritura. En producción, ejecutar en momentos de bajo tráfico.

4. **Health checks son opcionales**: `analyze_and_vacuum.py` puede ejecutar health checks, pero es mejor usar `db_health.py` directamente si necesitas esa funcionalidad.

---

## Conclusión

- **Usa `db_health.py`** cuando estés haciendo crawling/scraping y necesites detectar/reparar problemas del proceso.
- **Usa `analyze_and_vacuum.py`** para mantenimiento regular, verificar integridad y optimizar espacio.

Ambos scripts son complementarios y sirven propósitos diferentes en el ciclo de vida de la base de datos.
