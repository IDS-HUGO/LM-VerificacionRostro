"""Configuración del servicio, leída desde variables de entorno.

Todos los valores tienen un default seguro para desarrollo local, pero
`SIMILARITY_THRESHOLD` en particular DEBE recalibrarse con muestras reales
de INE/pasaporte mexicanos antes de usarse en producción (ver README.md).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=True, extra="ignore")

    # Umbral de decisión aplicado a la `distance` (coseno) devuelta por DeepFace.
    # match = distance <= SIMILARITY_THRESHOLD
    # Punto de partida razonable para ArcFace + cosine, NO calibrado con datos
    # reales de identificaciones mexicanas.
    SIMILARITY_THRESHOLD: float = 0.32

    # Backend de detección de rostros usado tanto para extraer el rostro de la
    # identificación como para verificar contra la selfie.
    DETECTOR_BACKEND: str = "retinaface"

    # Modelo de reconocimiento facial usado por DeepFace.verify.
    MODEL_NAME: str = "ArcFace"

    # Métrica de distancia usada por DeepFace.verify.
    DISTANCE_METRIC: str = "cosine"

    # Límite de tamaño de subida (por archivo), en megabytes. Se aplica ANTES
    # de decodificar la imagen, para evitar ataques tipo decompression-bomb.
    MAX_UPLOAD_SIZE_MB: int = 8

    # Puerto en el que corre uvicorn cuando se invoca este módulo directamente.
    PORT: int = 8000

    @property
    def max_upload_size_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Settings cacheados (singleton) para evitar releer el entorno en cada request."""
    return Settings()
