# Estrategia Two Towers para Base de Datos Lite

## Resumen Ejecutivo

Para la base de datos lite (`sputnik_lite.db`) en producción, **se recomienda usar el método de promedio de embeddings de releases** en lugar de la torre de usuarios entrenada localmente. Los embeddings de releases provienen de la base de datos grande y ofrecen mejor cobertura y calidad.

## Situación Actual

### Torre de Usuarios Entrenada

**Ubicación:** `models/user_tower_sputnik_lite.keras`

**Parámetros:**
- `num_artists`: 293 artistas entrenados
- `num_genres`: 97 géneros
- `embedding_dim`: 64
- `num_roles`: 10 (fijo)

**Estado:** Disponible para pruebas, pero **no recomendado para producción**

### Embeddings de Releases Precomputados

**Ubicación:** Tabla `release_embeddings_dl` en `sputnik_lite.db`

**Cantidad:** ~6,000 releases con embeddings

**Origen:** Generados desde la base de datos grande (`sputnik.db`) y copiados a la lite

**Estado:** **Recomendado para producción**

## Comparación de Estrategias

### Opción 1: Torre de Usuarios Entrenada Localmente

**Cómo funciona:**
- Entrena un modelo con los datos de la BD lite
- Genera embeddings de usuarios usando características del usuario
- Solo funciona para releases de los 293 artistas entrenados

**Limitaciones:**
- Solo 293 artistas entrenados (vs 126,948 en BD completa)
- Solo ~494 releases con embeddings generados localmente
- Requiere reentrenar cuando cambia la BD lite
- Calidad limitada por el tamaño reducido de la BD lite

### Opción 2: Método de Promedio (Recomendado)

**Cómo funciona:**
- Usa embeddings de releases precomputados de la BD grande
- Calcula embedding del usuario como promedio ponderado de sus releases calificados
- Funciona con cualquier release que tenga embedding

**Ventajas:**
- ~6,000 releases disponibles (vs 494 entrenados localmente)
- Embeddings de mejor calidad (entrenados con más datos)
- No requiere reentrenar cuando cambia la BD lite
- Más simple y robusto
- Funciona automáticamente para cualquier usuario con calificaciones

## Recomendación Final

### Para Producción (PythonAnywhere)

**Usar:** Método de promedio de embeddings de releases

**NO usar:** Torre de usuarios entrenada localmente

**Razones principales:**
1. **Mayor cobertura:** 6,000 releases vs 494
2. **Mejor calidad:** Embeddings entrenados con más datos de la BD grande
3. **Más robusto:** No falla si un artista no está entrenado
4. **Sin mantenimiento:** No requiere reentrenar cuando cambia la BD lite

### Para Desarrollo/Pruebas

**Puedes usar:** Torre de usuarios para pruebas locales si lo deseas

**Nota:** El modelo está disponible en `models/user_tower_sputnik_lite.keras` pero no es necesario para producción.

## Implementación Técnica

### Método de Promedio

**Ubicación:** `app/two_towers_update.py` - función `update_user_embedding()`

**Proceso:**
1. Obtiene releases calificados positivamente por el usuario (rating ≥ 3.0)
2. Carga embeddings de esos releases desde `release_embeddings_dl`
3. Calcula promedio ponderado:
   ```python
   # Peso según rating (rating más alto = más peso)
   weight = max(0.1, rating / 5.0)
   # Promedio ponderado
   user_embedding = weighted_average(release_embeddings, weights)
   # Normalización L2
   user_embedding = l2_normalize(user_embedding)
   ```
4. Guarda en `user_embeddings_dl`

**Ventajas del método:**
- No requiere modelo entrenado
- Funciona con cualquier release que tenga embedding
- Automático y robusto

### Torre de Usuarios

**Ubicación:** `models/user_tower_sputnik_lite.keras`

**Proceso:**
1. Carga el modelo entrenado
2. Extrae características del usuario (role, objectivity_score, etc.)
3. Genera embedding usando el modelo
4. Guarda en `user_embeddings_dl`

**Limitaciones:**
- Solo funciona para artistas entrenados (293)
- Requiere reentrenar cuando cambia la BD lite
- Menor cobertura que el método de promedio

## Flujo de Trabajo Recomendado

### Cuando actualices la BD lite:

1. **Generar embeddings en BD grande** (si es necesario):
   ```bash
   python -m offline_recommender.build_two_towers --database data/sputnik.db
   ```

2. **Crear BD lite** (copia embeddings de releases automáticamente):
   ```bash
   python scripts/build_lite_db.py --force
   ```
   Esto copia ~6,000 embeddings de releases de la BD grande a la lite.

3. **Los embeddings de usuarios se generan automáticamente**:
   - Cuando el usuario solicita recomendaciones avanzadas
   - Usando el método de promedio (no requiere modelo entrenado)
   - Funciona con cualquier release que tenga embedding

### NO necesitas:
- Reentrenar la torre de usuarios en la BD lite
- Generar embeddings de releases localmente
- Mantener el modelo `user_tower_sputnik_lite.keras` actualizado

## Conclusión

**Para la BD lite en producción, el método de promedio es superior** porque:
- Usa embeddings de mejor calidad (de la BD grande)
- Tiene mayor cobertura (~6,000 releases)
- Es más simple y robusto
- No requiere mantenimiento adicional

La torre de usuarios está disponible para pruebas pero **no es necesaria para producción**.

## Referencias

- Implementación del método de promedio: `app/two_towers_update.py`
- Script de construcción de BD lite: `scripts/build_lite_db.py`
- Documentación general de Two Towers: `docs/estrategias-recomendacion.md`
