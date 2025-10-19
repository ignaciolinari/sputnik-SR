"""Paquete Flask para la interfaz de recomendaciones de Sputnik."""

from .app import app


__all__ = ["app", "create_app"]


def create_app():
    """Devolver la instancia de Flask ya configurada."""
    return app
