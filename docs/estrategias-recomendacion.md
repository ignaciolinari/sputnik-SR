# Estrategias de Recomendación

Este documento describe todas las estrategias de recomendación implementadas en el sistema Sputnik-SR.

## Tabla de Contenidos

1. [Max-Ensemble](#max-ensemble)
2. [Motor Híbrido](#motor-híbrido)
3. [Co-ocurrencia (release_pairs)](#co-ocurrencia-release_pairs)
4. [Recomendaciones Avanzadas (NMF + Two Towers)](#recomendaciones-avanzadas-nmf--two-towers)
5. [Factorización Matricial (NMF)](#factorización-matricial-nmf)
6. [Two Towers (Deep Learning)](#two-towers-deep-learning)
7. [Perfiles de Contenido](#perfiles-de-contenido)
8. [Popularidad](#popularidad)
9. [Exploración Aleatoria](#exploración-aleatoria)
10. [Recomendaciones Contextuales](#recomendaciones-contextuales)
11. [Métricas de Evaluación](#métricas-de-evaluación)

---

## Max-Ensemble

**Función:** `recommend_max_ensemble(user_id, limit=9)`
**Estado:** Experimental - En evaluación
**API Endpoint:** `/api/recommend/<user_id>/max_ensemble`

Sistema experimental que combina múltiples estrategias seleccionando el **score máximo** para cada release candidato. A diferencia del híbrido anterior que selecciona UNA estrategia, max-ensemble ejecuta TODAS las estrategias disponibles y toma lo mejor de cada una.

### Mecánica

1. **Genera candidatos** con cada estrategia disponible:
   - `pairs`: 3x candidatos (peso alto, mejor para 64.7% usuarios)
   - `content`: 1.5x candidatos (peso medio, mejor para 26.3% usuarios)
   - `advanced`: 1x candidatos (peso base, mejor para 6.5% usuarios)

2. **Para cada release candidato**, guarda el score MÁS ALTO entre todas las estrategias

3. **Ordena todos los releases** por su score máximo

4. **Diversifica por artista** y retorna top-K

### Adaptabilidad Automática

El sistema se adapta según las interacciones del usuario:

| Interacciones | Estrategias Activas | Comportamiento |
|---------------|---------------------|----------------|
| **0** | Popular | Cold start (igual que híbrido) |
| **1-19** | pairs + content | Combina co-ocurrencia y contenido |
| **20+** | pairs + content + advanced | Todas las estrategias disponibles |

### Ejemplo Concreto

Usuario con 25 ratings positivos:

```
pairs genera:    Release A: 0.95, Release B: 0.80
content genera:  Release B: 0.70, Release C: 0.85
advanced genera: Release A: 0.50, Release D: 0.75

Max-Ensemble combina:
  A → MAX(0.95, 0.50) = 0.95 ✓ (preserva el mejor)
  C → 0.85 ✓
  B → MAX(0.80, 0.70) = 0.80 ✓
  D → 0.75 ✓

Ranking final: [A, C, B, D, ...]
```

### Ventajas sobre Híbrido anterior

1. **No elige una sola estrategia:** Aprovecha fortalezas de todas simultáneamente
2. **Sin promedios que diluyan:** Preserva los mejores scores de cada estrategia
3. **Diversidad implícita:** Diferentes releases pueden venir de diferentes estrategias
4. **Sin hiperparámetros:** No requiere calibrar pesos ni umbrales

### ¿Por qué funciona mejor?

**Complementariedad real:** Diferentes estrategias son mejores para diferentes usuarios:
- `pairs` funciona mejor para 64.7% de usuarios
- `content` funciona mejor para 26.3% de usuarios
- `advanced` funciona mejor para 6.5% de usuarios

El máximo captura el "ganador" para cada release, mientras que el híbrido anterior elegía una sola estrategia para todo el usuario.

### Uso

```python
# Via Python
from app import recommender
recommendations = recommender.recommend_max_ensemble("user_id", limit=9)

# Via API
GET /api/recommend/<user_id>/max_ensemble?limit=9&format=full
```

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

3. **Con 9 calificaciones positivas:**
   - Prioriza perfiles de contenido (`recommend_content_based`)
   - Si faltan candidatos, completa con popularidad
   - Si aún faltan, agrega aleatorios

4. **Con ≥20 calificaciones positivas:**
   - Prioriza recomendaciones avanzadas (`recommend_advanced`) si embeddings disponibles
     - **Nivel 1 (20-29 calificaciones)**: Usa solo NMF
     - **Nivel 2 (≥30 calificaciones)**: Combina NMF + Two Towers con pesos y bonus de consenso
   - Si recomendaciones avanzadas no disponibles, usa perfiles de contenido como fallback
   - Si faltan candidatos, completa con popularidad
   - Si aún faltan, agrega aleatorios

5. **Diversificación final:**
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

## Recomendaciones Avanzadas (NMF + Two Towers)

**Función:** `recommend_advanced(user_id, limit=9)`

Sistema unificado que combina NMF y Two Towers según el nivel del usuario. Se activa automáticamente para usuarios con ≥20 calificaciones positivas cuando los embeddings están disponibles.

### Niveles de Desbloqueo

El sistema tiene dos niveles progresivos:

- **Nivel 1 (≥20 calificaciones positivas)**: Activa solo NMF
- **Nivel 2 (≥30 calificaciones positivas)**: Activa combinación de NMF + Two Towers

### Lógica de Combinación (Nivel 2)

Cuando ambos sistemas están disponibles en nivel 2:

1. **Obtiene candidatos de ambos sistemas:**
   - NMF: top `limit * 3` candidatos
   - Two Towers: top `limit * 3` candidatos

2. **Normaliza scores por posición:**
   - Cada candidato recibe un score normalizado [0, 1] basado en su posición
   - Mejor posición = score más alto (1.0 para el primero)

3. **Combina scores con pesos:**
   ```python
   combined_score = (
       0.4 * nmf_score +           # 40% peso para NMF
       0.6 * two_towers_score      # 60% peso para Two Towers
   )
   ```

4. **Aplica bonus de consenso:**
   - Si un candidato aparece en ambos sistemas: `+0.2` al score combinado
   - Esto prioriza recomendaciones con consenso entre sistemas

5. **Ordena y retorna top-k:**
   - Ordena por score combinado descendente
   - Retorna los top `limit` candidatos

### Fallbacks Inteligentes

- Si solo NMF está disponible: usa solo NMF
- Si solo Two Towers está disponible: usa solo Two Towers
- Si ninguno está disponible: retorna lista vacía (fallback a content-based en el motor híbrido)

### Actualización Bajo Demanda

Los usuarios pueden actualizar sus embeddings desde la interfaz web usando el botón unificado **"Recomendaciones avanzadas"**:

- **Nivel 0 (< 20 ratings)**: Botón deshabilitado, muestra progreso "15/20"
- **Nivel 1 (20-29 ratings)**: Botón habilitado, actualiza solo NMF
- **Nivel 2 (≥30 ratings)**: Botón habilitado, actualiza NMF + Two Towers

**Endpoint:** `POST /actualizar-recomendaciones-avanzadas`

El endpoint detecta automáticamente el nivel del usuario y actualiza los sistemas correspondientes.

### Ventajas del Sistema Unificado

- ✅ **UX simplificada**: Un solo botón en lugar de dos técnicos
- ✅ **Progresión clara**: El usuario ve su progreso hacia el siguiente nivel
- ✅ **Mejor calidad**: La combinación en nivel 2 aprovecha lo mejor de ambos sistemas
- ✅ **Consenso**: El bonus de consenso prioriza recomendaciones más confiables
- ✅ **Fallbacks robustos**: Funciona incluso si un sistema falla

### Configuración

- **`min_advanced_level_1_signals`**: `20` - Umbral para nivel 1 (NMF)
- **`min_advanced_level_2_signals`**: `30` - Umbral para nivel 2 (NMF + Two Towers)
- **`advanced_nmf_weight`**: `0.4` - Peso de NMF en combinación
- **`advanced_two_towers_weight`**: `0.6` - Peso de Two Towers en combinación
- **`advanced_consensus_bonus`**: `0.2` - Bonus para candidatos en ambos sistemas

---

## Factorización Matricial (NMF)

**Función:** `recommend_nmf(user_id, limit=9)`

Sistema basado en **Non-negative Matrix Factorization (NMF)** que aprende patrones latentes de las preferencias de los usuarios. Se activa automáticamente como parte del sistema de **Recomendaciones Avanzadas** para usuarios con ≥20 calificaciones positivas cuando los embeddings están disponibles. En nivel 1 se usa solo NMF, y en nivel 2 se combina con Two Towers.

### Construcción de Embeddings (Offline)

**Script:** `offline_recommender/build_nmf_embeddings.py`

#### Proceso:

1. **Filtrado de datos:**
   - Filtra interacciones con `rating >= 3.0` (configurable)
   - Incluye solo usuarios con ≥10 calificaciones positivas (configurable)
   - Incluye solo releases con ≥5 calificaciones positivas (configurable)

2. **Construcción de matriz sparse:**
   - Crea matriz usuario-ítem en formato CSR (Compressed Sparse Row)
   - Solo almacena valores no-cero (muy eficiente en memoria)
   - Típicamente usa ~50-100 MB para datasets grandes

3. **Entrenamiento NMF:**
   - Factoriza matriz como: `matriz ≈ user_embeddings @ item_embeddings.T`
   - Aprende factores latentes (default: 50 componentes)
   - Usa regularización L1/L2 para evitar sobreajuste
   - Converge típicamente en 50-100 iteraciones

4. **Almacenamiento:**
   - Guarda embeddings de usuarios en tabla `user_embeddings`
   - Guarda embeddings de releases en tabla `release_embeddings`
   - Cada embedding es un vector de factores latentes (JSON array)

### Recomendación en Tiempo Real

#### Algoritmo `recommend_nmf()`:

1. **Carga embedding del usuario:**
   - Busca en `user_embeddings` el embedding del usuario
   - Si no existe, retorna lista vacía (fallback a content-based)

2. **Calcula similitud coseno:**
   - Para cada release con embedding disponible:
     ```python
     similarity = dot(user_embedding, release_embedding) /
                  (norm(user_embedding) * norm(release_embedding))
     ```

3. **Filtra y ordena:**
   - Excluye releases ya vistos o calificados por el usuario
   - Ordena por similitud descendente
   - Retorna top-k recomendaciones

### Configuración Actual

- **Umbral de activación:** `min_nmf_signals = 20` (usuarios con ≥20 calificaciones positivas)
- **Componentes latentes:** `n_components = 50` (configurable en construcción)
- **Filtros de datos:** `min_user_ratings = 10`, `min_release_ratings = 5` (configurable)
- **Iteraciones máximas:** `max_iter = 200` (típicamente converge antes)

### Ejemplo Práctico

**Usuario con 25 calificaciones positivas:**

1. Sistema híbrido detecta que tiene ≥20 calificaciones
2. Intenta usar `recommend_nmf()`
3. Carga embedding del usuario (vector de 50 factores)
4. Calcula similitud con ~109k releases con embeddings
5. Retorna top 9 recomendaciones más similares

**Si embeddings no disponibles:**
- Fallback automático a `recommend_content_based()`
- Sistema sigue funcionando normalmente

### Ventajas

- ✅ **Captura patrones complejos**: Los factores latentes descubren relaciones no obvias
- ✅ **Muy eficiente en memoria**: Matrices sparse usan ~50-100 MB vs ~17 GB densas
- ✅ **Escalable**: Inferencia rápida (<100ms) incluso con muchos releases
- ✅ **Mejor para usuarios activos**: Funciona mejor con más datos del usuario
- ✅ **Diversidad**: Puede descubrir releases fuera de géneros/artistas obvios

### Limitaciones

- ⚠️ **Requiere embeddings precomputados**: Deben generarse offline periódicamente
- ⚠️ **Cold start**: No funciona para usuarios nuevos (<20 calificaciones)
- ⚠️ **Depende de calidad de datos**: Requiere suficientes interacciones positivas
- ⚠️ **Menos interpretable**: Los factores latentes no tienen significado directo

### Actualización de Embeddings

Los embeddings pueden actualizarse de dos formas:

#### 1. Actualización Bajo Demanda (Recomendado)

Los usuarios con ≥20 calificaciones positivas pueden generar o actualizar su embedding individual desde la interfaz web usando el botón unificado **"Recomendaciones avanzadas"** junto a su nombre de usuario. El sistema detecta automáticamente el nivel y actualiza NMF (nivel 1) o NMF + Two Towers (nivel 2).

**Ventajas:**
- ✅ Actualización inmediata cuando el usuario califica nuevos discos
- ✅ Solo recalcula el embedding del usuario (muy rápido, <1 segundo)
- ✅ No requiere acceso al servidor ni scripts offline
- ✅ Disponible directamente desde la interfaz web

**Cómo funciona:**
- El sistema calcula un promedio ponderado de los embeddings de releases que el usuario calificó positivamente
- Usa los embeddings de releases precomputados (que deben existir)
- Guarda el nuevo embedding del usuario en la base de datos

#### 2. Actualización Periódica Offline (Para Releases)

Los embeddings de releases deben reconstruirse periódicamente cuando haya nuevas interacciones en el sistema:

```bash
# Reconstruir embeddings de releases (típicamente semanal)
python -m offline_recommender.build_nmf_embeddings \
    --n-components 50 \
    --min-user-ratings 10 \
    --min-release-ratings 5 \
    --verbose
```

**Tiempo estimado**: 1-2 minutos para datasets medianos/grandes

**Nota**: Los usuarios pueden actualizar sus embeddings individuales en cualquier momento desde la interfaz, pero los embeddings de releases deben regenerarse offline periódicamente para incluir nuevos releases y actualizar los patrones latentes globales.

### Comparación con Otras Estrategias

| Aspecto | NMF | Content-based | Release Pairs |
|---------|-----|---------------|--------------|
| **Complejidad** | Alta | Media | Baja |
| **Cold start** | Malo | Bueno | Bueno |
| **Usuarios activos** | Excelente | Bueno | Regular |
| **Diversidad** | Alta | Media | Baja |
| **Interpretabilidad** | Baja | Alta | Media |
| **Memoria** | Media | Baja | Baja |

---

## Two Towers (Deep Learning)

**Función:** `recommend_two_towers(user_id, limit=9)`

Sistema basado en **aprendizaje profundo** que utiliza una arquitectura Two Towers para aprender embeddings separados de usuarios e items basándose en sus características y preferencias. Se activa automáticamente como parte del sistema de **Recomendaciones Avanzadas** en nivel 2 (≥30 calificaciones positivas), donde se combina con NMF para mejores recomendaciones.

### Arquitectura

El modelo consiste en dos redes neuronales separadas:

1. **Torre de Usuario (User Tower)**
   - Codifica características del usuario (role, objectivity_score, soundoffs, ratings_count, días desde registro/actividad)
   - Output: Embedding de usuario (vector de dimensión configurable, default: 64)

2. **Torre de Items (Item Tower)**
   - Codifica características del release (artist_id, release_type, géneros, año, avg_rating, ratings_count)
   - Output: Embedding de release (vector de misma dimensión)

3. **Scoring**
   - Producto escalar entre embeddings normalizados (equivalente a similitud coseno)
   - Los embeddings se normalizan con L2 para eficiencia

### Construcción de Embeddings (Offline)

**Script:** `offline_recommender/build_two_towers.py`

#### Proceso:

1. **Filtrado de datos:**
   - Filtra interacciones con `rating >= 3.0` (configurable)
   - Incluye solo usuarios con ≥5 calificaciones positivas (configurable)
   - Incluye solo releases con ≥3 calificaciones positivas (configurable)
   - Opcionalmente limita con `--sample-size` para pruebas rápidas

2. **Extracción de features:**
   - **Usuarios**: role, objectivity_score, soundoffs, ratings_count, días desde registro/actividad
   - **Releases**: artist_id, release_type, géneros (multi-hot), release_year, avg_rating, ratings_count
   - Normalización y transformaciones (log, escalado, etc.)

3. **Entrenamiento del modelo:**
   - Arquitectura con Keras/TensorFlow
   - Embeddings categóricos (role, artist, type, genres)
   - Capas densas para features numéricas
   - Dropout para regularización
   - Sampling de negativos configurable (`--num-negatives`, default 4) para construir pares positivo/negativo por usuario
   - Loss: Binary Cross-Entropy con logits + class weights (da más peso a los positivos)
   - Métricas online: `binary_accuracy` y `AUC` para monitorear convergencia
   - Optimizador: Adam con learning rate configurable
   - Callbacks: Early stopping y ReduceLROnPlateau (monitoreando AUC)

4. **Almacenamiento:**
   - Guarda embeddings de usuarios en tabla `user_embeddings_dl`
   - Guarda embeddings de releases en tabla `release_embeddings_dl`
   - Cada embedding incluye dimensión y versión del modelo

### Recomendación en Tiempo Real

#### Algoritmo `recommend_two_towers()`:

1. **Carga embedding del usuario:**
   - Busca en `user_embeddings_dl` el embedding del usuario
   - Si no existe, retorna lista vacía (fallback a content-based)

2. **Calcula similitud:**
   - Para cada release con embedding disponible:
     ```python
     # Embeddings ya están L2-normalizados, producto escalar = coseno
     similarity = dot(user_embedding, release_embedding)
     ```

3. **Filtra y ordena:**
   - Excluye releases ya vistos o calificados por el usuario
   - Ordena por similitud descendente
   - Retorna top-k recomendaciones

### Configuración Actual

- **Umbral de activación:** Se activa en nivel 2 de recomendaciones avanzadas (≥30 calificaciones positivas)
- **Dimensión de embeddings:** `embedding_dim = 64` (configurable)
- **Filtros de datos:** `min_user_ratings = 5`, `min_release_ratings = 3` (configurable)
- **Épocas:** `epochs = 10` (configurable, con early stopping)
- **Batch size:** `batch_size = 1024` (configurable)

**Nota:** Two Towers ahora se usa principalmente en combinación con NMF en el sistema de recomendaciones avanzadas nivel 2, aprovechando las fortalezas de ambos sistemas.

### Ejemplo Práctico

**Usuario con 35 calificaciones positivas (Nivel 2):**

1. Sistema híbrido detecta que tiene ≥30 calificaciones (nivel 2)
2. Usa `recommend_advanced()` que combina NMF + Two Towers
3. Obtiene candidatos de ambos sistemas
4. Combina scores con pesos (40% NMF, 60% Two Towers)
5. Aplica bonus de consenso (+0.2) a candidatos que aparecen en ambos
6. Retorna top 9 recomendaciones con mejor score combinado

**Si embeddings no disponibles:**
- Fallback automático a `recommend_content_based()`
- Sistema sigue funcionando normalmente

### Ventajas

- ✅ **Mejor uso de features**: Aprovecha características de usuario e items que NMF no usa
- ✅ **Cold start mejorado**: Puede hacer recomendaciones usando features estáticas sin historial extenso
- ✅ **Patrones no lineales**: Las redes neuronales pueden capturar relaciones complejas
- ✅ **Flexibilidad**: Fácil agregar nuevas features sin cambiar la arquitectura
- ✅ **Escalable**: Inferencia rápida con embeddings precomputados
- ✅ **Complementario**: Puede coexistir con NMF y usarse según el caso

### Limitaciones

- ⚠️ **Requiere embeddings precomputados**: Deben generarse offline periódicamente
- ⚠️ **Tiempo de entrenamiento**: Más lento que NMF (minutos vs segundos)
- ⚠️ **Hiperparámetros**: Requiere tuning de arquitectura, learning rate, etc.
- ⚠️ **Dependencias**: Requiere TensorFlow/Keras
- ⚠️ **Memoria**: Modelo más pesado que NMF (aunque embeddings son similares)
- ⚠️ **Menos interpretable**: Los embeddings no tienen significado directo

### Entrenamiento del Modelo

Los embeddings deben generarse offline usando el script de construcción:

```bash
# Entrenamiento completo (recomendado)
python -m offline_recommender.build_two_towers \
    --database data/sputnik.db \
    --embedding-dim 64 \
    --epochs 10 \
    --batch-size 1024 \
    --min-user-ratings 5 \
    --min-release-ratings 3 \
    --verbose

# Prueba rápida con subconjunto
python -m offline_recommender.build_two_towers \
    --database data/sputnik.db \
    --sample-size 50000 \
    --epochs 5 \
    --verbose
```

**Tiempo estimado**:
- Prueba rápida (50k interacciones): ~30 segundos
- Entrenamiento completo (8M interacciones): 3-6 horas en CPU

**Checkpoints y reanudación:**
- `--checkpoint-path`: guarda los mejores pesos del modelo combinado al final de cada epoch (solo weights). Recomendado apuntar a `models/Two Towers/checkpoints/*.weights.h5`.
- `--resume-from-checkpoint`: carga esos pesos antes de entrenar y continúa la corrida (mantiene el estado del optimizador). Útil si la máquina se reinicia o si querés seguir refinando un modelo previo sin empezar desde cero.

Con esto podés dividir entrenamientos largos en múltiples sesiones sin perder el progreso.

### Evaluación automática con NDCG@k

El mismo script puede reservar interacciones para un holdout por usuario y calcular NDCG@k sin necesidad de correr `evaluate_recommender.py`.

- Activalo con `--evaluate-ndcg`. El parámetro `--ndcg-holdout` (default 0.2) define qué fracción de interacciones positivas se reserva por usuario y `--ndcg-min-test-items` asegura que cada usuario tenga suficientes ítems en el holdout.
- `--ndcg-k` controla el tamaño del ranking evaluado (default 9) y `--ndcg-max-users` permite limitar la cantidad de usuarios evaluados para corridas rápidas.
- El pipeline separa las interacciones, entrena el modelo sobre el split de entrenamiento y luego evalúa usando los towers recién entrenados. La métrica se registra en `models/Two Towers/two_towers_<db>_metadata.json` junto con el número de usuarios evaluados y los parámetros de holdout.
- Esto replica el flujo automatizado de NMF (que optimiza con NDCG) y deja trazabilidad de cada corrida para comparar modelos sin lanzar scripts adicionales.

### Actualización de Embeddings

Los embeddings pueden actualizarse de dos formas:

#### 1. Actualización Bajo Demanda (Recomendado)

Los usuarios con ≥20 calificaciones positivas pueden generar o actualizar sus embeddings desde la interfaz web usando el botón unificado **"Recomendaciones avanzadas"** junto a su nombre de usuario. El sistema detecta automáticamente el nivel y actualiza los sistemas correspondientes (NMF en nivel 1, NMF + Two Towers en nivel 2).

**Ventajas:**
- ✅ Actualización inmediata cuando el usuario califica nuevos discos
- ✅ Intenta usar el modelo entrenado si está disponible, sino usa aproximación por promedio ponderado
- ✅ No requiere acceso al servidor ni scripts offline
- ✅ Disponible directamente desde la interfaz web

**Cómo funciona:**
- Si el modelo entrenado está disponible, genera el embedding usando las características del usuario
- Si el modelo no está disponible, calcula un promedio ponderado de los embeddings de releases que el usuario calificó positivamente
- Usa los embeddings de releases precomputados (que deben existir)
- Guarda el nuevo embedding del usuario en la base de datos

#### 2. Actualización Periódica Offline (Para Releases y Modelo)

Los embeddings de releases y el modelo deben reconstruirse periódicamente cuando haya nuevas interacciones en el sistema:

```bash
# Reconstruir embeddings y modelo (típicamente semanal)
python -m offline_recommender.build_two_towers \
    --database data/sputnik.db \
    --embedding-dim 64 \
    --epochs 10 \
    --batch-size 1024 \
    --min-user-ratings 5 \
    --min-release-ratings 3 \
    --verbose
```

**Tiempo estimado**: 3-6 horas para datasets grandes (en CPU)

**Nota**: Los usuarios pueden actualizar sus embeddings individuales en cualquier momento desde la interfaz. El modelo y los embeddings de releases deben regenerarse offline periódicamente para incluir nuevos releases y actualizar los patrones aprendidos globalmente.

### Comparación con NMF

| Aspecto | NMF | Two Towers |
|---------|-----|------------|
| **Umbral mínimo** | 20 ratings | 10 ratings |
| **Features** | Solo ratings | Múltiples features |
| **Cold start** | Malo | Mejor (usa features) |
| **Entrenamiento** | Rápido (~1-2 min) | Más lento (~10-30 min) |
| **Inferencia** | Muy rápida | Rápida |
| **Patrones** | Lineales | No lineales |
| **Interpretabilidad** | Baja | Baja |

---

## Perfiles de Contenido

**Función:** `recommend_content_based(user_id, limit=9)`

Sistema que construye un perfil del usuario basado en géneros y artistas de sus discos favoritos. Se activa automáticamente cuando el usuario tiene 9 calificaciones positivas, o como fallback si las recomendaciones avanzadas no están disponibles para usuarios con más calificaciones.

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
- **`min_advanced_level_1_signals`**: `20` - Umbral para activar recomendaciones avanzadas nivel 1 (NMF)
- **`min_advanced_level_2_signals`**: `30` - Umbral para activar recomendaciones avanzadas nivel 2 (NMF + Two Towers)
- **`advanced_nmf_weight`**: `0.6` - Peso de NMF en combinación nivel 2
- **`advanced_two_towers_weight`**: `0.4` - Peso de Two Towers en combinación nivel 2
- **`advanced_consensus_bonus`**: `0.2` - Bonus para candidatos que aparecen en ambos sistemas
- **`min_two_towers_signals`**: `10` - Umbral legacy (mantenido para compatibilidad)
- **`min_nmf_signals`**: `20` - Umbral legacy (mantenido para compatibilidad)
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
| **Recomendaciones Avanzadas** | ≥20 calificaciones | Muy Alta | Alta |
| **NMF** | Nivel 1 avanzadas (20-29) | Muy Alta | Alta |
| **Two Towers** | Nivel 2 avanzadas (≥30) | Muy Alta | Alta |
| **Contenido** | 9 calificaciones, fallback | Alta | Media |
| **Popularidad** | Fallback | Baja | Baja |
| **Aleatorio** | Último fallback | Ninguna | Baja |
| **Contextual** | Página de disco | Media | Media |

---

## Estrategias Futuras

### Filtrado Colaborativo Basado en Usuarios (User-Based CF)

**Estado**: No implementado - Consideración futura

El sistema actual utiliza filtrado colaborativo basado en items (item-based CF) a través de la tabla `release_pairs`. Una posible extensión sería implementar filtrado colaborativo basado en usuarios (user-based CF).

#### Concepto

En lugar de encontrar discos similares a los que el usuario calificó (item-based), user-based CF encuentra usuarios similares al usuario objetivo y recomienda discos que esos usuarios similares calificaron positivamente.

#### Ventajas Potenciales

- **Descubrimiento más diverso**: Puede cruzar géneros y encontrar relaciones indirectas entre discos
- **Mejor para usuarios con historial largo**: Aprovecha las preferencias de usuarios similares con más datos
- **Adaptación a cambios de gusto**: Al recalcular similitudes periódicamente, puede reflejar cambios en preferencias

#### Desventajas y Consideraciones

- **Escalabilidad**: Requiere calcular y mantener similitudes usuario-usuario (matriz O(n²) usuarios)
- **Dependencia de overlap**: Necesita suficiente overlap entre usuarios para encontrar vecinos útiles
- **Cold start**: No funciona bien para usuarios nuevos sin historial suficiente
- **Complejidad operativa**: Requiere mantenimiento periódico de similitudes y estrategias de caching

#### Cuándo Considerar Implementación

User-based CF sería beneficioso si:
- Hay suficiente overlap promedio entre usuarios (≥15-20% Jaccard similarity)
- Existe un número significativo de usuarios con historial largo (≥20-50 calificaciones positivas)
- La densidad de la matriz usuario-ítem es suficiente para encontrar vecinos útiles
- Se puede mantener la infraestructura para precomputar similitudes offline

#### Implementación Sugerida (si se decide implementar)

1. **Precomputación offline**: Similar a `release_pairs`, construir tabla `user_similarities` con:
   - Similitud coseno o Pearson entre vectores de ratings de usuarios
   - Top-K vecinos más similares por usuario
   - Solo para usuarios con suficiente historial (≥20 calificaciones positivas)

2. **Estrategia híbrida**:
   - Usar user-based CF solo para usuarios con ≥50 calificaciones positivas
   - Mantener item-based como estrategia principal para el resto
   - Combinar ambas señales para usuarios con historial muy largo

3. **Script de análisis**: `offline_recommender/analyze_user_cf_potential.py` puede evaluar si la implementación sería beneficiosa

#### Referencias Técnicas

- Algoritmo: Encontrar K usuarios más similares usando similitud coseno/Pearson sobre ratings centrados
- Optimización: Precomputar offline, cachear en memoria, actualizar periódicamente
- Evaluación: Comparar NDCG@k con sistema actual antes de implementar en producción

---

## Referencias

- Implementación: `app/recommender.py`
- Recomendaciones avanzadas: `recommend_advanced()` - combina NMF + Two Towers
- Actualización de embeddings bajo demanda: `app/nmf_update.py`, `app/two_towers_update.py`
- Endpoint unificado: `POST /actualizar-recomendaciones-avanzadas` en `app/app.py`
- Construcción de pares: `offline_recommender/build_release_pairs.py`
- Construcción de embeddings NMF: `offline_recommender/build_nmf_embeddings.py`
- Construcción de embeddings Two Towers: `offline_recommender/build_two_towers.py`
- Evaluación: `offline_recommender/evaluate_recommender.py`
- Métricas: `app/metrics.py`
- Análisis NMF: `docs/analisis-svd-nmf.md`
- Guía NMF: `docs/guia-nmf.md`
- Análisis de potencial user-based CF: `offline_recommender/analyze_user_cf_potential.py`
