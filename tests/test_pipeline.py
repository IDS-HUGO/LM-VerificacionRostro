"""Pruebas unitarias de app/pipeline.py.

La mayoría de estas pruebas NO invocan DeepFace real (usan la función pura
`decide_match` o mocks). La única prueba que sí requiere DeepFace/los pesos
del modelo reales está marcada `@pytest.mark.slow` y se auto-omite si el
paquete no está disponible en el entorno (ver README.md del servicio).
"""
import time

import pytest

from app.config import Settings
from app.pipeline import (
    InvalidImageError,
    NoFaceOnIdError,
    NoFaceOnSelfieError,
    TransientModelError,
    _classify_exception,
    _with_retry,
    decide_match,
    decode_image,
    run_verification,
)


class TestDecideMatch:
    """Tabla de casos en el límite exacto del umbral (`<=`, no `<`)."""

    @pytest.mark.parametrize(
        "distance,threshold,expected",
        [
            (0.0, 0.32, True),        # distancia mínima posible
            (0.10, 0.32, True),       # claramente por debajo
            (0.31999, 0.32, True),    # justo debajo
            (0.32, 0.32, True),       # exactamente en el umbral -> match (<=)
            (0.32001, 0.32, False),   # justo arriba
            (0.5, 0.32, False),       # claramente por encima
            (1.0, 0.32, False),       # distancia máxima típica (cosine)
        ],
    )
    def test_table_driven(self, distance, threshold, expected):
        assert decide_match(distance, threshold) is expected


class TestDecodeImage:
    def test_valid_image_decodes_to_array(self, solid_color_image_bytes):
        image = decode_image(solid_color_image_bytes)
        assert image is not None
        assert image.ndim == 3
        assert image.shape[2] == 3

    def test_corrupt_bytes_raise_invalid_image_error(self, corrupt_image_bytes):
        with pytest.raises(InvalidImageError):
            decode_image(corrupt_image_bytes)

    def test_empty_bytes_raise_invalid_image_error(self):
        with pytest.raises(InvalidImageError):
            decode_image(b"")


class TestClassifyException:
    def test_transient_infra_message_is_reclassified(self):
        original = ValueError("Connection reset while downloading model weights")
        classified = _classify_exception(original)
        assert isinstance(classified, TransientModelError)

    def test_unrelated_message_is_not_reclassified(self):
        original = ValueError("algo totalmente distinto e inesperado")
        classified = _classify_exception(original)
        assert classified is original


class TestWithRetry:
    def test_succeeds_without_retry(self):
        calls = []

        def func():
            calls.append(1)
            return "ok"

        assert _with_retry(func) == "ok"
        assert len(calls) == 1

    def test_retries_on_transient_error_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise TransientModelError("modelo aún cargando")
            return "ok-eventualmente"

        result = _with_retry(flaky)
        assert result == "ok-eventualmente"
        assert attempts["count"] == 3
        # 2 reintentos -> 2 backoffs (500ms, 1500ms)
        assert sleeps == [0.5, 1.5]

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)

        def always_fails():
            raise TransientModelError("nunca se recupera")

        with pytest.raises(TransientModelError):
            _with_retry(always_fails)

    def test_no_face_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("no debería reintentar")))

        calls = {"count": 0}

        def raises_no_face():
            calls["count"] += 1
            raise ValueError("Face could not be detected in numpy array.")

        with pytest.raises(ValueError):
            _with_retry(raises_no_face)
        assert calls["count"] == 1


@pytest.mark.slow
def test_run_verification_real_deepface_no_face_images(solid_color_image_bytes, another_solid_color_image_bytes):
    """Prueba de extremo a extremo con el DeepFace real (no mockeado).

    Se omite automáticamente si `deepface` no está instalado/importable en
    este entorno (p. ej. sandbox sin internet para descargar pesos, o una
    versión de Python para la que `tensorflow`/`deepface` aún no publican
    wheels). Usa imágenes sintéticas sin rostro a propósito: no requiere
    ningún dataset de rostros real y de todas formas ejercita el camino real
    de `DeepFace.extract_faces` fallando por ausencia de rostro.
    """
    pytest.importorskip("deepface")

    settings = Settings(SIMILARITY_THRESHOLD=0.32)
    with pytest.raises(NoFaceOnIdError):
        run_verification(solid_color_image_bytes, another_solid_color_image_bytes, settings)
