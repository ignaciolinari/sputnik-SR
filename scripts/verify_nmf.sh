#!/bin/bash
# Script para verificar que NMF está configurado correctamente

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DB_PATH="${SPUTNIK_DB:-$PROJECT_ROOT/data/sputnik.db}"

echo "=== Verificación de NMF ==="
echo ""

# Verificar dependencias
echo "1. Verificando dependencias Python..."
if python -c "import numpy; import sklearn" 2>/dev/null; then
    echo "  ✓ numpy y scikit-learn instalados"
else
    echo "  ✗ Faltan dependencias. Ejecuta: pip install numpy scikit-learn"
    exit 1
fi

# Verificar base de datos
echo ""
echo "2. Verificando base de datos..."
if [ ! -f "$DB_PATH" ]; then
    echo "  ✗ Base de datos no encontrada: $DB_PATH"
    exit 1
fi
echo "  ✓ Base de datos encontrada: $DB_PATH"

# Verificar tablas
echo ""
echo "3. Verificando tablas de embeddings..."
USER_TABLE_EXISTS=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='user_embeddings';" 2>/dev/null || echo "0")
RELEASE_TABLE_EXISTS=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='release_embeddings';" 2>/dev/null || echo "0")

if [ "$USER_TABLE_EXISTS" = "1" ]; then
    echo "  ✓ Tabla user_embeddings existe"
else
    echo "  ✗ Tabla user_embeddings no existe. Ejecuta: sqlite3 $DB_PATH < data/schema.sql"
fi

if [ "$RELEASE_TABLE_EXISTS" = "1" ]; then
    echo "  ✓ Tabla release_embeddings existe"
else
    echo "  ✗ Tabla release_embeddings no existe. Ejecuta: sqlite3 $DB_PATH < data/schema.sql"
fi

# Verificar embeddings
echo ""
echo "4. Verificando embeddings generados..."
USER_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM user_embeddings;" 2>/dev/null || echo "0")
RELEASE_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM release_embeddings;" 2>/dev/null || echo "0")

if [ "$USER_COUNT" -gt 0 ] && [ "$RELEASE_COUNT" -gt 0 ]; then
    echo "  ✓ Embeddings encontrados:"
    echo "    - Usuarios: $USER_COUNT"
    echo "    - Releases: $RELEASE_COUNT"

    # Verificar fecha de actualización
    LAST_UPDATE=$(sqlite3 "$DB_PATH" "SELECT MAX(last_updated) FROM user_embeddings;" 2>/dev/null || echo "N/A")
    echo "    - Última actualización: $LAST_UPDATE"
else
    echo "  ✗ No se encontraron embeddings."
    echo "    Ejecuta: python -m offline_recommender.build_nmf_embeddings"
fi

# Verificar función de recomendación
echo ""
echo "5. Verificando función de recomendación..."
cd "$PROJECT_ROOT"
if python -c "from app import recommender; assert hasattr(recommender, 'recommend_nmf')" 2>/dev/null; then
    echo "  ✓ Función recommend_nmf() disponible"
else
    echo "  ✗ Función recommend_nmf() no encontrada"
    exit 1
fi

echo ""
echo "=== Verificación completada ==="
echo ""
if [ "$USER_COUNT" -gt 0 ] && [ "$RELEASE_COUNT" -gt 0 ]; then
    echo "✓ NMF está listo para usar"
    echo ""
    echo "Para probar:"
    echo "  python -c \"from app import recommender; print(recommender.recommend_nmf('tu_usuario_id', 9))\""
else
    echo "⚠ NMF necesita ser configurado. Sigue los pasos en docs/estrategias-recomendacion.md (sección NMF)"
    echo ""
    echo "Próximo paso:"
    echo "  python -m offline_recommender.build_nmf_embeddings --verbose"
fi
