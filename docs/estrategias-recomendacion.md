# Estrategias de Recomendación

Este documento describe todas las estrategias de recomendación implementadas en el sistema Sputnik-SR.

## Tabla de Contenidos

1. [Motor Híbrido](#motor-híbrido)
2. [Co-ocurrencia (release_pairs)](#co-ocurrencia-release_pairs)
3. [Perfiles de Contenido](#perfiles-de-contenido)
4. [Popularidad](#popularidad)
5. [Exploración Aleatoria](#exploración-aleatoria)
6. [Recomendaciones Contextuales](#recomendaciones-contextuales)
7. [Métricas de Evaluación](#métricas-de-evaluación)

---

## Motor Híbrido

**Función:** `recommend(user_id, limit=9)`

El motor híbrido es la estrategia principal que combina todas las demás estrategias según el historial del usuario. Selecciona automáticamente la mejor estrategia disponible y usa fallbacks cuando es necesario.

### Lógica de Decisión

1. **Sin calificaciones positivas:**
   - Usa recomendaciones por popularidad
   - Si faltan candidatos, completa con aleatorios

2. **Con ≤8 calificaciones positivas:**
   - Prioriza co-ocurrencia (`recommend_from_pairs`)
   - Si faltan candidatos, completa con popularidad
   - Si aún faltan, agrega aleatorios

3. **Con >8 calificaciones positivas:**
   - Prioriza perfiles de contenido (`recommend_content_based`)
   - Si faltan candidatos, completa con popularidad
   - Si aún faltan, agrega aleatorios

4. **Diversificación final:**
   - Aplica diversificación por artista (`_diversify_by_artist`)
   - Prioriza discos de artistas diferentes
   - Evita repetir el mismo artista en las recomendaciones

### Ejemplo de Flujo

```
Usuario con 5 calificaciones positivas:
1. Intenta co-ocurrencia → encuentra 6 candidatos
2. Completa con popularidad → encuentra 3 más
3. Diversifica por artista → reordena para evitar repeticiones
4. Devuelve top 9 recomendaciones
```

---

## Co-ocurrencia (release_pairs)

**Función:** `recommend_from_pairs(user_id, limit=9)`

Sistema basado en la frecuencia con que los discos aparecen juntos en las colecciones de los usuarios. Ideal para usuarios con pocas calificaciones (≤8).

### Construcción de la Tabla `release_pairs` (Offline)

**Script:** `offline_recommender/build_release_pairs.py`

#### Proceso:

1. **Análisis de interacciones positivas:**
   - Filtra todas las interacciones con `rating >= 3.0` (configurable)
   - Crea una tabla temporal con `(usuario, disco)` para calificaciones positivas

2. **Cálculo de co-ocurrencias:**
   - Para cada par de discos (A, B), cuenta cuántos usuarios calificaron ambos
   - Procesa en lotes para eficiencia (batch_size=250 por defecto)

3. **Cálculo de métricas:**
   - **`pair_count`**: Cantidad de usuarios que calificaron ambos discos
   - **`jaccard`**: Similitud entre conjuntos de usuarios
     ```
     jaccard = pair_count / (usuarios_A + usuarios_B - pair_count)
     ```
   - **`lift`**: Medida de asociación estadística
     ```
     lift = pair_count / (usuarios_A * usuarios_B)
     ```

4. **Filtrado:**
   - Solo guarda pares con `pair_count >= 3` (configurable con `--min-pair-count`)
   - La tabla es bidireccional: si existe (A, B), también existe (B, A)

### Recomendación en Tiempo Real

#### Algoritmo `_score_pairs()`:

1. **Obtiene anchors:** Los discos que el usuario calificó positivamente

2. **Busca relaciones:** Encuentra todos los discos relacionados en `release_pairs`

3. **Calcula score para cada candidato:**
   ```python
   score = rating_weight * recency_weight * pair_count *
           (0.7 + 0.3 * lift) * (0.5 + 0.5 * jaccard)
   ```

   **Componentes:**
   - **`rating_weight`**: `max(0.1, rating / 5.0)` - Peso según el rating del disco anchor
   - **`recency_weight`**: `1 / log2(días_desde_calificación + 1)` - Peso por recencia
   - **`pair_count`**: Frecuencia de co-ocurrencia (más usuarios = más confianza)
   - **`lift`**: Factor de sorpresa (0.7 base + 0.3 * lift)
   - **`jaccard`**: Similitud de usuarios (0.5 base + 0.5 * jaccard)

4. **Acumula scores:** Si un disco aparece relacionado con varios anchors, suma los scores

5. **Ordena y filtra:** Devuelve los top N excluyendo los ya vistos

### Ejemplo Práctico

**Usuario calificó:**
- Disco A: rating 4.5, hace 10 días
- Disco B: rating 3.5, hace 5 días

**En `release_pairs`:**
- (A, X): pair_count=50, lift=2.0, jaccard=0.3
- (B, X): pair_count=30, lift=1.5, jaccard=0.2

**Cálculo del score para X:**
```
Desde A: (4.5/5) * recency_A * 50 * (0.7 + 0.3*2.0) * (0.5 + 0.5*0.3)
        = 0.9 * 0.85 * 50 * 1.3 * 0.65 ≈ 32.3

Desde B: (3.5/5) * recency_B * 30 * (0.7 + 0.3*1.5) * (0.5 + 0.5*0.2)
        = 0.7 * 0.92 * 30 * 1.15 * 0.6 ≈ 13.3

Score total para X = 32.3 + 13.3 = 45.6
```

### Ventajas

- ✅ Funciona bien con pocas señales (hasta 8 calificaciones)
- ✅ Encuentra conexiones directas entre discos
- ✅ Considera rating y recencia del usuario
- ✅ Usa métricas estadísticas (lift, jaccard) para filtrar ruido

### Limitaciones

- ⚠️ Depende de la calidad de `release_pairs` (requiere reconstrucción periódica)
- ⚠️ Con muchas señales puede volverse ruidoso
- ⚠️ Menos generalizable que el sistema basado en contenido

### Configuración

- **Umbral de activación:** `max_pairs_signals = 8` (usa co-ocurrencia con ≤8 calificaciones)
- **Mínimo de pares:** `min_pair_count = 3` (en construcción de tabla)
- **Mínimo de rating:** `min_rating = 3.0` (para considerar positiva una interacción)

---

## Perfiles de Contenido

**Función:** `recommend_content_based(user_id, limit=9)`

Sistema que construye un perfil del usuario basado en géneros y artistas de sus discos favoritos. Se activa automáticamente cuando el usuario tiene más de 8 calificaciones positivas.

### Construcción del Perfil (`_user_profile`)

1. **Análisis de calificaciones positivas:**
   - Filtra interacciones con `rating >= 3.0`
   - Para cada interacción positiva:

2. **Cálculo de pesos:**
   - **`rating_weight`**: `max(0.1, rating / 5.0)` - Peso según el rating
   - **`recency_weight`**: `1 / log2(días_desde_calificación + 1)` - Peso por recencia
   - **`base_weight`**: `rating_weight * recency_weight`

3. **Distribución de pesos:**
   - Extrae géneros del disco (`release_genres`)
   - Extrae artista del disco (`release_artist_id`)
   - Distribuye `base_weight` entre géneros (si hay múltiples géneros, divide el peso)
   - Asigna `base_weight` completo al artista

4. **Resultado:**
   ```python
   {
       "genres": {genre_id: peso_acumulado, ...},
       "artists": {artist_id: peso_acumulado, ...},
       "total_weight": suma_total
   }
   ```

### Generación de Candidatos

1. **Pool por géneros:**
   - Busca todos los discos que pertenecen a los géneros del perfil
   - Limite: `limit * candidate_pool_multiplier` (default: 5x)

2. **Pool por artistas:**
   - Busca todos los discos de los artistas del perfil
   - Limite: `limit * candidate_pool_multiplier`

3. **Combinación:**
   - Une ambos pools (usa `set` para evitar duplicados)
   - Excluye discos ya vistos o calificados

### Scoring (`_content_score`)

Para cada candidato, calcula:

```python
score = (peso_géneros * genre_weight) +
        (peso_artista * artist_weight) +
        (popularity_prior * popularidad_score)
```

**Componentes:**

1. **Peso por géneros:**
   ```python
   for genre_id in release_genres:
       score += genre_weight * profile["genres"].get(genre_id, 0.0)
   ```

2. **Peso por artista:**
   ```python
   if artist_id in profile["artists"]:
       score += artist_weight * profile["artists"][artist_id]
   ```

3. **Prior de popularidad:**
   ```python
   popularity_score = (
       0.6 * (avg_rating / 5.0) +
       0.3 * log1p(ratings_count) +
       0.1 * recent_bonus
   )
   ```

   Donde `recent_bonus` considera la antigüedad del disco:
   ```python
   years_old = año_actual - release_year
   recent_bonus = max(0.0, 1.0 - (years_old / 50.0))
   ```

### Configuración Actual

- **`genre_weight`**: `1.0` - Peso de los géneros en el score
- **`artist_weight`**: `0.8` - Peso de los artistas (ligeramente menor que géneros)
- **`popularity_prior`**: `0.3` - Peso del factor de popularidad
- **`candidate_pool_multiplier`**: `5` - Multiplicador para el pool de candidatos

### Ventajas

- ✅ Generaliza bien con más datos del usuario
- ✅ No depende de co-ocurrencias específicas
- ✅ Considera múltiples factores (géneros, artistas, popularidad)
- ✅ Funciona mejor con usuarios con historial más extenso

### Limitaciones

- ⚠️ Necesita suficientes calificaciones para construir un perfil robusto
- ⚠️ Puede ser sesgado si el usuario solo califica un género muy específico
- ⚠️ Menos personalizado que co-ocurrencia para usuarios nuevos

---

## Popularidad

**Función:** `_popular_unseen_releases(user_id, limit)`

Estrategia de fallback que recomienda discos populares que el usuario aún no ha visto. Se usa cuando las estrategias principales no generan suficientes candidatos.

### Algoritmo

1. **Consulta SQL:**
   ```sql
   SELECT r.id_release
   FROM releases AS r
   LEFT JOIN interactions AS i
       ON i.id_release = r.id_release AND i.id_user = ?
   WHERE i.id_user IS NULL
   ORDER BY
       (r.ratings_count IS NULL),
       r.ratings_count DESC,
       (r.avg_rating IS NULL),
       r.avg_rating DESC,
       r.id_release DESC
   LIMIT ?;
   ```

2. **Ordenamiento:**
   - Primero por `ratings_count` (más calificaciones = más popular)
   - Luego por `avg_rating` (mejor rating promedio)
   - Excluye discos ya interactuados por el usuario

### Uso

- Se activa automáticamente como fallback cuando:
  - Las estrategias principales no generan suficientes candidatos
  - El usuario no tiene calificaciones positivas

### Ventajas

- ✅ Simple y rápido
- ✅ Funciona para usuarios nuevos
- ✅ Asegura que siempre haya recomendaciones disponibles

### Limitaciones

- ⚠️ No es personalizado
- ⚠️ Puede recomendar discos muy conocidos que el usuario ya conoce
- ⚠️ No considera preferencias del usuario

---

## Exploración Aleatoria

**Función:** `recommend_random(user_id, limit=9)`

Estrategia que selecciona discos aleatorios del catálogo para fomentar la exploración y diversidad.

### Algoritmo

1. **Consulta SQL:**
   ```sql
   SELECT r.id_release
   FROM releases AS r
   WHERE NOT EXISTS (
       SELECT 1
       FROM interactions AS i
       WHERE i.id_release = r.id_release AND i.id_user = ?
   );
   ```

2. **Selección aleatoria:**
   - Obtiene todos los discos no vistos
   - Usa `random.sample()` para seleccionar `limit` discos aleatorios

### Uso

- Se activa como último fallback cuando:
  - Las estrategias principales no generan suficientes candidatos
  - Se necesita diversidad en las recomendaciones

### Ventajas

- ✅ Fomenta la exploración
- ✅ Descubre discos fuera de la zona habitual del usuario
- ✅ Aumenta la diversidad del catálogo recomendado

### Limitaciones

- ⚠️ No considera preferencias del usuario
- ⚠️ Puede recomendar discos de baja calidad
- ⚠️ Menor relevancia esperada

---

## Recomendaciones Contextuales

**Función:** `recommend_context(user_id, release_id, limit=3)`

Sistema especializado para recomendar discos relacionados con un disco específico. Se usa en páginas de detalle de discos.

### Estrategia Multi-Fuente

1. **Recomendaciones directas (`release_recommendations`):**
   - Busca en la tabla de recomendaciones pre-calculadas
   - Ordena por popularidad (`ratings_count`, `avg_rating`)

2. **Co-ocurrencias (`release_pairs`):**
   - Si faltan candidatos, busca en `release_pairs`
   - Ordena por `pair_count` descendente

3. **Discografía del artista:**
   - Si aún faltan candidatos, busca otros discos del mismo artista
   - Ordena por año de lanzamiento (más recientes primero)

4. **Fallback de popularidad:**
   - Si aún faltan candidatos, completa con discos populares

### Filtrado

- Excluye discos ya vistos o calificados por el usuario
- Excluye el disco actual (`release_id`)
- Elimina duplicados

### Ventajas

- ✅ Múltiples fuentes de información
- ✅ Especializado para contexto específico
- ✅ Considera relaciones directas e indirectas

### Limitaciones

- ⚠️ Depende de la calidad de las tablas de relaciones
- ⚠️ Puede ser limitado si el disco tiene pocas relaciones

---

## Métricas de Evaluación

**Módulo:** `app/metrics.py`

El sistema implementa métricas estándar de evaluación de sistemas de recomendación basadas en ranking.

### DCG (Discounted Cumulative Gain)

**Función:** `discounted_cumulative_gain(relevance_scores)`

Mide la calidad del ranking considerando la posición de los elementos relevantes.

```python
DCG = Σ(relevance_i / log2(i + 2))
```

- Los elementos en posiciones más altas tienen más peso
- El descuento aumenta logarítmicamente con la posición

### IDCG (Ideal Discounted Cumulative Gain)

**Función:** `ideal_discounted_cumulative_gain(relevance_scores)`

Es el DCG del ranking ideal (elementos ordenados por relevancia descendente).

### NDCG (Normalized Discounted Cumulative Gain)

**Función:** `normalized_discounted_cumulative_gain(relevance_scores)`

Normaliza el DCG por el IDCG para obtener un valor entre 0 y 1.

```python
NDCG = DCG / IDCG
```

- **1.0**: Ranking perfecto
- **0.0**: Ranking sin elementos relevantes o peor que aleatorio

### Evaluación Offline

**Script:** `offline_recommender/evaluate_recommender.py`

Evalúa los sistemas de recomendación usando holdout de interacciones:

1. **Selección de usuarios:** Usuarios con al menos `min_ratings` calificaciones
2. **Split de datos:** Separa `holdout_ratio` de las interacciones para testing
3. **Evaluación:** Calcula NDCG@k para cada estrategia:
   - Hybrid
   - Pairs
   - Content-based
   - Random
   - Popular
4. **Reporte:** Genera CSV con resultados detallados por usuario

### Uso

```bash
python -m offline_recommender.evaluate_recommender \
    --min-ratings 50 \
    --sample-size 100 \
    --holdout-ratio 0.2 \
    --k 9 \
    --output eval_results.csv
```

---

## Configuración Global

**Clase:** `Config` en `app/recommender.py`

### Parámetros Principales

- **`positive_rating_threshold`**: `3.0` - Rating mínimo para considerar positiva una interacción
- **`max_pairs_signals`**: `8` - Umbral para cambiar de co-ocurrencia a contenido
- **`genre_weight`**: `1.0` - Peso de géneros en scoring de contenido
- **`artist_weight`**: `0.8` - Peso de artistas en scoring de contenido
- **`popularity_prior`**: `0.3` - Peso del factor de popularidad
- **`recency_log_base`**: `2.0` - Base del logaritmo para cálculo de recencia
- **`popularity_recent_divisor`**: `50.0` - Divisor para cálculo de bonus de recencia
- **`pairs_limit_multiplier`**: `3` - Multiplicador para límite de pares
- **`pairs_table_sample`**: `10` - Muestra de tabla de pares
- **`candidate_pool_multiplier`**: `5` - Multiplicador para pool de candidatos

---

## Resumen de Estrategias

| Estrategia | Cuándo se usa | Personalización | Complejidad |
|------------|---------------|-----------------|-------------|
| **Motor Híbrido** | Siempre (principal) | Alta | Alta |
| **Co-ocurrencia** | ≤8 calificaciones | Media-Alta | Media |
| **Contenido** | >8 calificaciones | Alta | Media |
| **Popularidad** | Fallback | Baja | Baja |
| **Aleatorio** | Último fallback | Ninguna | Baja |
| **Contextual** | Página de disco | Media | Media |

---

## Referencias

- Implementación: `app/recommender.py`
- Construcción de pares: `offline_recommender/build_release_pairs.py`
- Evaluación: `offline_recommender/evaluate_recommender.py`
- Métricas: `app/metrics.py`
