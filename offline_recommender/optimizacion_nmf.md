# Optimización de Hiperparámetros para NMF

Este documento explica cómo funciona la optimización bayesiana de hiperparámetros para el modelo NMF (Non-negative Matrix Factorization) y cómo utilizarla.

## Tabla de Contenidos

1. [Introducción](#introducción)
2. [¿Qué es la Optimización Bayesiana?](#qué-es-la-optimización-bayesiana)
3. [Hiperparámetros Optimizados](#hiperparámetros-optimizados)
4. [Modos de Uso](#modos-de-uso)
5. [Ejemplos Prácticos](#ejemplos-prácticos)
6. [Cómo Funciona Internamente](#cómo-funciona-internamente)
7. [Checkpoints y Reanudación](#checkpoints-y-reanudación)
8. [Recomendaciones](#recomendaciones)
9. [Troubleshooting](#troubleshooting)

---

## Introducción

El script `build_nmf_embeddings.py` ahora incluye optimización bayesiana de hiperparámetros para encontrar automáticamente los mejores parámetros del modelo NMF. Esto mejora significativamente la calidad de las recomendaciones sin necesidad de ajuste manual.

### ¿Por qué optimizar hiperparámetros?

Los hiperparámetros del modelo NMF tienen un impacto directo en la calidad de las recomendaciones:

- **`n_components`**: Número de factores latentes. Demasiados pueden causar sobreajuste, muy pocos pueden perder información importante.
- **`alpha_W` y `alpha_H`**: Regularización L2 para embeddings de usuarios e items. Controlan el trade-off entre ajuste y generalización.
- **`l1_ratio`**: Balance entre regularización L1 y L2. L1 puede ayudar con sparsity, L2 con estabilidad.
- **`max_iter`**: Número máximo de iteraciones. Más iteraciones pueden mejorar la convergencia pero aumentan el tiempo de entrenamiento.

Encontrar los valores óptimos manualmente es difícil y requiere mucho tiempo. La optimización bayesiana automatiza este proceso.

---

## ¿Qué es la Optimización Bayesiana?

La optimización bayesiana es una técnica de optimización global que:

1. **Construye un modelo probabilístico** (Gaussian Process) de la función objetivo basándose en evaluaciones previas
2. **Selecciona inteligentemente** el siguiente punto a evaluar usando una función de adquisición (Expected Improvement)
3. **Converge más rápido** que métodos aleatorios o grid search, especialmente en espacios de búsqueda grandes

### Ventajas sobre Grid Search

- **Más eficiente**: Requiere menos evaluaciones para encontrar buenos parámetros
- **Escala mejor**: Funciona bien en espacios de búsqueda grandes y continuos
- **Balance exploración/explotación**: Explora áreas prometedoras mientras mantiene diversidad

---

## Hiperparámetros Optimizados

El script optimiza los siguientes hiperparámetros dentro de estos rangos:

| Hiperparámetro | Rango | Tipo | Descripción |
|----------------|-------|------|-------------|
| `n_components` | 10 - 100 | Entero | Número de factores latentes |
| `max_iter` | 50 - 500 | Entero | Máximo de iteraciones de entrenamiento |
| `alpha_W` | 1e-5 - 1e-1 | Real (log-uniform) | Regularización L2 para embeddings de usuarios |
| `alpha_H` | 1e-5 - 1e-1 | Real (log-uniform) | Regularización L2 para embeddings de items |
| `l1_ratio` | 0.0 - 1.0 | Real | Balance L1/L2 (0.0 = solo L2, 1.0 = solo L1) |

### Notas sobre los Rangos

- **`alpha_W` y `alpha_H`**: Usan distribución log-uniform porque los valores óptimos suelen estar en órdenes de magnitud diferentes (ej: 0.0001 vs 0.01)
- **`n_components`**: El rango 10-100 es un buen balance entre capacidad del modelo y riesgo de sobreajuste
- **`max_iter`**: El rango permite modelos rápidos (50 iteraciones) y modelos más refinados (500 iteraciones)

---

## Modos de Uso

El script soporta tres modos de operación:

### 1. Entrenamiento Estándar (Sin Optimización)

Entrena el modelo con parámetros especificados manualmente:

```bash
python -m offline_recommender.build_nmf_embeddings \
    --n-components 50 \
    --max-iter 200 \
    --alpha-w 0.001 \
    --alpha-h 0.001 \
    --l1-ratio 0.0
```

**Cuándo usar**: Cuando ya conoces buenos parámetros o quieres resultados rápidos sin optimización.

### 2. Solo Optimizar Hiperparámetros

Encuentra los mejores hiperparámetros sin entrenar el modelo final:

```bash
python -m offline_recommender.build_nmf_embeddings \
    --optimize \
    --n-calls 30 \
    --save-params nmf_optimal_params.json
```

**Cuándo usar**: Cuando quieres explorar qué parámetros funcionan mejor antes de hacer el entrenamiento final.

### 3. Optimizar y Entrenar

Optimiza hiperparámetros y luego entrena el modelo final con los mejores parámetros encontrados:

```bash
python -m offline_recommender.build_nmf_embeddings \
    --optimize-and-train \
    --n-calls 30
```

**Cuándo usar**: Cuando quieres el mejor modelo posible y tienes tiempo para la optimización.

### 4. Entrenar con Parámetros Previamente Optimizados

Carga parámetros desde un archivo JSON y entrena el modelo:

```bash
python -m offline_recommender.build_nmf_embeddings \
    --load-params nmf_optimal_params.json
```

**Cuándo usar**: Cuando ya optimizaste previamente y quieres reentrenar con los mismos parámetros óptimos.

---

## Ejemplos Prácticos

### Ejemplo 1: Primera Optimización

Si es la primera vez que optimizas, ejecuta:

```bash
# Paso 1: Optimizar y guardar parámetros con NDCG (recomendado)
# Puede tomar 2-4 horas para datasets grandes (recomendado usar checkpoints)
python -m offline_recommender.build_nmf_embeddings \
    --optimize \
    --n-calls 50 \
    --metric ndcg \
    --ndcg-k 9 \
    --checkpoint-dir checkpoints/nmf_optimization \
    --save-params nmf_params.json \
    --verbose

# Paso 2: Revisar los parámetros encontrados
cat nmf_params.json

# Paso 3: Entrenar modelo final con parámetros óptimos
python -m offline_recommender.build_nmf_embeddings \
    --load-params nmf_params.json
```

**Nota**: Si necesitas una optimización más rápida para exploración inicial, puedes usar `--metric mse`, pero los resultados pueden no ser tan buenos para recomendaciones.

**Importante**: Para datasets grandes, siempre usa `--checkpoint-dir` para poder reanudar si se interrumpe.

### Ejemplo 2: Optimización Rápida para Pruebas

Para una optimización rápida usando MSE (más rápido pero menos preciso):

```bash
python -m offline_recommender.build_nmf_embeddings \
    --optimize-and-train \
    --n-calls 15 \
    --metric mse \
    --min-user-ratings 20 \
    --min-release-ratings 10
```

Para optimización con NDCG (recomendado, más lento pero mejor):

```bash
python -m offline_recommender.build_nmf_embeddings \
    --optimize-and-train \
    --n-calls 15 \
    --metric ndcg \
    --ndcg-k 9 \
    --min-user-ratings 20 \
    --min-release-ratings 10
```

### Ejemplo 3: Reentrenamiento Periódico

Si ya tienes parámetros optimizados y solo quieres reentrenar:

```bash
python -m offline_recommender.build_nmf_embeddings \
    --load-params nmf_params.json \
    --min-user-ratings 15 \
    --min-release-ratings 10
```

### Ejemplo 4: Optimización con Base de Datos Específica

```bash
python -m offline_recommender.build_nmf_embeddings \
    --database data/sputnik_lite.db \
    --optimize-and-train \
    --n-calls 30 \
    --metric ndcg \
    --ndcg-k 9 \
    --save-params nmf_lite_params.json
```

### Ejemplo 5: Optimización con Checkpoints (Recomendado para datasets grandes)

Para optimizaciones largas que pueden interrumpirse, usa checkpoints:

```bash
# Paso 1: Iniciar optimización con checkpoints
python -m offline_recommender.build_nmf_embeddings \
    --optimize \
    --n-calls 30 \
    --metric ndcg \
    --checkpoint-dir checkpoints/nmf_optimization \
    --save-params nmf_params.json \
    --verbose

# Si se interrumpe después de 15 iteraciones...

# Paso 2: Reanudar desde checkpoint
python -m offline_recommender.build_nmf_embeddings \
    --optimize \
    --n-calls 30 \
    --metric ndcg \
    --resume-from checkpoints/nmf_optimization/nmf_optimization_checkpoint.pkl \
    --checkpoint-dir checkpoints/nmf_optimization \
    --save-params nmf_params.json \
    --verbose

# Esto completará las 15 iteraciones restantes (total 30)
```

**Ventajas de usar checkpoints:**
- No pierdes progreso si se interrumpe la optimización
- Puedes pausar y reanudar cuando quieras
- Los checkpoints se guardan automáticamente después de cada iteración
- Combina resultados previos y nuevos automáticamente

---

## Cómo Funciona Internamente

### Proceso de Optimización

1. **Carga de Datos**: Carga la matriz usuario-item desde la base de datos aplicando los filtros especificados (`min_rating`, `min_user_ratings`, `min_release_ratings`)

2. **División Train/Test**: Divide los usuarios en conjunto de entrenamiento (80%) y prueba (20%) para evaluar la generalización

3. **Espacio de Búsqueda**: Define los rangos de cada hiperparámetro usando `skopt`

4. **Función Objetivo**: Para cada combinación de hiperparámetros:
   - Entrena un modelo NMF en el conjunto de entrenamiento
   - Evalúa el error de reconstrucción en el conjunto de prueba
   - Retorna el error (negativo para maximización)

5. **Optimización Bayesiana**: Usa Gaussian Process para:
   - Modelar la relación entre hiperparámetros y error
   - Seleccionar el siguiente punto a evaluar usando Expected Improvement
   - Converger hacia los mejores parámetros

6. **Resultado**: Retorna el diccionario con los mejores hiperparámetros encontrados

### Métricas de Evaluación

El script soporta dos métricas de evaluación:

#### 1. MSE (Mean Squared Error) - Por defecto en versiones anteriores

Mide el error de reconstrucción de la matriz:

```
MSE = mean((test_matrix - reconstructed_matrix)²)
```

**Ventajas:**
- **Rápida**: Evalúa directamente la reconstrucción sin generar recomendaciones
- **Estándar**: Ampliamente usada en factorización matricial
- **Eficiente**: No requiere calcular recomendaciones para cada usuario

**Desventajas:**
- **No evalúa calidad de recomendaciones directamente**: MSE mide reconstrucción, no ranking
- **Puede no correlacionar bien con calidad**: Un modelo con bajo MSE no garantiza buenas recomendaciones

#### 2. NDCG@k (Normalized Discounted Cumulative Gain) - **Recomendada**

Evalúa directamente la calidad de las recomendaciones:

1. Entrena modelo en conjunto de entrenamiento
2. Genera top-k recomendaciones para cada usuario de test
3. Compara con items reales del conjunto de test (holdout)
4. Calcula NDCG@k promedio

**Ventajas:**
- **Evalúa calidad de recomendaciones directamente**: Mide qué tan bien el modelo rankea items relevantes
- **Alineada con objetivo final**: Optimiza directamente para generar buenas recomendaciones
- **Métrica estándar en sistemas de recomendación**: Ampliamente usada en investigación y producción

**Desventajas:**
- **Más lenta**: Requiere generar recomendaciones para cada usuario en cada iteración
- **Requiere más memoria**: Necesita mantener información de usuarios e items

### ¿Cuál usar?

**Recomendación: Usa NDCG@k (por defecto)**

- Es la métrica por defecto desde la versión actualizada
- Está directamente alineada con el objetivo de generar buenas recomendaciones
- Aunque es más lenta, vale la pena para obtener mejores resultados

**Usa MSE solo si:**
- Tienes limitaciones de tiempo muy estrictas
- Estás haciendo una exploración inicial rápida
- El dataset es extremadamente grande y NDCG es prohibitivamente lento

---

## Checkpoints y Reanudación

### ¿Por qué usar checkpoints?

Para optimizaciones largas (especialmente con NDCG en datasets grandes), los checkpoints son esenciales:

- **Protección contra interrupciones**: Si se corta la luz o el proceso, no pierdes progreso
- **Flexibilidad**: Puedes pausar y reanudar cuando quieras
- **Tiempo valioso**: Con datasets grandes, cada iteración puede tomar varios minutos

### Cómo usar checkpoints

1. **Iniciar con checkpoints**:
   ```bash
   --checkpoint-dir checkpoints/nmf_optimization
   ```
   Esto guarda automáticamente el progreso después de cada iteración en `checkpoints/nmf_optimization/nmf_optimization_checkpoint.pkl`

2. **Reanudar si se interrumpe**:
   ```bash
   --resume-from checkpoints/nmf_optimization/nmf_optimization_checkpoint.pkl \
   --checkpoint-dir checkpoints/nmf_optimization
   ```
   El sistema detecta cuántas iteraciones ya se completaron y continúa desde ahí.

### Notas importantes sobre checkpoints

- **Mismo `--n-calls`**: Usa el mismo número total de iteraciones al reanudar (el sistema calcula cuántas faltan)
- **Mismos parámetros**: Usa los mismos `--metric`, `--ndcg-k`, etc. al reanudar
- **Ubicación del checkpoint**: El archivo se guarda en `{checkpoint_dir}/nmf_optimization_checkpoint.pkl`
- **Merge automático**: Los resultados previos y nuevos se combinan automáticamente

---

## Recomendaciones

### Número de Iteraciones (`--n-calls`)

- **15-20 iteraciones**: Rápido, bueno para exploración inicial
- **30-50 iteraciones**: Balance recomendado entre tiempo y calidad
- **50+ iteraciones**: Para máxima calidad, puede tomar varias horas

**Recomendación**: Empieza con 30 iteraciones. Si los resultados mejoran significativamente en las últimas iteraciones, considera aumentar a 50.

**Para datasets grandes**: Siempre usa `--checkpoint-dir` porque la optimización puede tomar horas.

### Cuándo Re-optimizar

Re-optimiza los hiperparámetros cuando:

- **Cambios significativos en los datos**: Muchos nuevos usuarios o releases
- **Cambios en filtros**: Modificaste `min_user_ratings` o `min_release_ratings`
- **Mejoras en el sistema**: Cambios en cómo se procesan los datos
- **Periódicamente**: Cada 3-6 meses para mantener parámetros actualizados

**No necesitas re-optimizar** si solo estás reentrenando con los mismos datos y filtros.

### Filtros de Datos

Los filtros (`min_user_ratings`, `min_release_ratings`) afectan qué datos se usan para optimización:

- **Más restrictivos** (valores altos): Datos más limpios pero menos datos → optimización más rápida pero menos representativa
- **Menos restrictivos** (valores bajos): Más datos pero más ruido → optimización más lenta pero más representativa

**Recomendación**: Usa los mismos filtros que usarás en producción.

### Dependencias

La optimización requiere `scikit-optimize`:

```bash
pip install scikit-optimize
```

Si no está instalado, el script mostrará un error claro y seguirá funcionando en modo estándar (sin optimización).

---

## Troubleshooting

### Error: "scikit-optimize is required"

**Solución**: Instala scikit-optimize:
```bash
pip install scikit-optimize
```

### La optimización toma mucho tiempo

**Causas posibles**:
- Muchos datos (`min_user_ratings` y `min_release_ratings` muy bajos)
- Muchas iteraciones (`--n-calls` muy alto)
- Hardware lento

**Soluciones**:
- Aumenta `min_user_ratings` y `min_release_ratings` para reducir el tamaño de la matriz
- Reduce `--n-calls` a 15-20 para optimización más rápida
- Usa `--optimize` primero, luego `--load-params` para separar optimización y entrenamiento

### Los parámetros optimizados no mejoran las recomendaciones

**Posibles causas**:
- La métrica de optimización (MSE) no correlaciona perfectamente con calidad de recomendaciones
- Los datos de entrenamiento/test no son representativos
- Necesitas más iteraciones de optimización

**Soluciones**:
- Aumenta `--n-calls` para exploración más exhaustiva
- Verifica que los filtros sean apropiados para tu caso de uso
- Considera evaluar con `evaluate_recommender.py` para métricas más relevantes (NDCG)

### Error al reanudar desde checkpoint

**Causas posibles**:
- Cambiaste parámetros entre la corrida original y el resume (ej: `--metric`, `--ndcg-k`)
- El archivo de checkpoint está corrupto o incompleto
- Usaste diferentes filtros de datos (`--min-user-ratings`, etc.)

**Soluciones**:
- Usa exactamente los mismos parámetros al reanudar (excepto `--resume-from`)
- Verifica que el archivo de checkpoint existe y no está corrupto
- Si el checkpoint está corrupto, puedes empezar de nuevo o usar `--n-calls` menor para completar menos iteraciones

### El checkpoint no se guarda

**Causas posibles**:
- No especificaste `--checkpoint-dir`
- Problemas de permisos en el directorio
- Disco lleno

**Soluciones**:
- Siempre especifica `--checkpoint-dir` para optimizaciones largas
- Verifica permisos de escritura en el directorio
- Verifica espacio en disco disponible

---

## Referencias

- **Script principal**: `offline_recommender/build_nmf_embeddings.py`
- **Documentación scikit-optimize**: https://scikit-optimize.github.io/
- **Documentación NMF sklearn**: https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.NMF.html
- **Estrategias de recomendación**: `docs/estrategias-recomendacion.md`
