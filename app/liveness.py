"""Punto de extensión para detección de vida (liveness / anti-spoofing).

Hoy este módulo NO implementa liveness real: siempre devuelve
`status="skipped"`. Existe para que el resto del pipeline (main.py,
pipeline.py) ya tenga la forma correcta de la respuesta y no requiera un
cambio de contrato cuando se implemente liveness de verdad.

Ruta de mejora recomendada (NO implementada aquí):
    DeepFace.extract_faces(..., anti_spoofing=True) ya existe en la librería
    DeepFace y devuelve, por cada rostro detectado, una clave adicional
    `is_real` (bool) y `antispoof_score` (float) en el dict de resultado.
    Cuando se priorice liveness, `check_liveness` debería:
      1. Recibir también el resultado de `DeepFace.extract_faces` con
         `anti_spoofing=True` (o volver a llamarlo con ese flag).
      2. Mapear `is_real` -> status "passed"/"failed" y `antispoof_score` ->
         score.
      3. Documentar cómo afecta esto al fail-closed contract (p. ej. ¿un
         liveness "failed" debe bloquear el match aunque la similitud sea
         alta? -- decisión de producto pendiente).
"""
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np


@dataclass(frozen=True)
class LivenessResult:
    status: Literal["skipped", "passed", "failed"]
    score: Optional[float] = None


def check_liveness(selfie_array: "np.ndarray") -> LivenessResult:
    """Stub de liveness. Actualmente siempre "skipped": no se realiza ninguna
    verificación real de que la selfie provenga de una persona viva (y no de
    una foto de una foto, una pantalla, una máscara, etc.).

    Parámetros
    ----------
    selfie_array:
        Imagen de la selfie ya decodificada en memoria (numpy BGR), la misma
        que usa el resto del pipeline. No se usa todavía, pero se recibe para
        que la firma ya sea la correcta cuando se implemente liveness real.
    """
    del selfie_array  # no usado todavía; ver docstring del módulo.
    return LivenessResult(status="skipped", score=None)
