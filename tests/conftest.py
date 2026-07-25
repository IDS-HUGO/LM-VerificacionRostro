"""Fixtures compartidas. Genera imágenes sintéticas (colores sólidos) en
memoria para ejercitar los caminos de "no hay rostro"/"imagen corrupta" sin
depender de datasets de rostros reales ni de archivos binarios en el repo.
"""
import cv2
import numpy as np
import pytest


def encode_solid_color_image(color=(10, 10, 10), width: int = 200, height: int = 200) -> bytes:
    """Genera una imagen JPEG sintética de color sólido, codificada a bytes.
    Es una imagen válida (decodifica correctamente) pero sin ningún rostro,
    útil para ejercitar el camino NO_FACE_* sin invocar DeepFace de verdad."""
    image = np.full((height, width, 3), color, dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok, "no se pudo codificar la imagen sintética de prueba"
    return buffer.tobytes()


@pytest.fixture
def solid_color_image_bytes() -> bytes:
    return encode_solid_color_image()


@pytest.fixture
def another_solid_color_image_bytes() -> bytes:
    return encode_solid_color_image(color=(200, 200, 200))


@pytest.fixture
def corrupt_image_bytes() -> bytes:
    """Bytes que no representan ninguna imagen válida."""
    return b"esto-no-es-una-imagen-" + b"\x00\xff" * 32
