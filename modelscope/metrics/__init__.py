# Copyright (c) Alibaba, Inc. and its affiliates.
from typing import TYPE_CHECKING

from modelscope.utils.import_utils import LazyImportModule

if TYPE_CHECKING:
    from .base import Metric
    from .builder import METRICS, build_metric, task_default_metrics
    from .image_color_enhance_metric import ImageColorEnhanceMetric
    from .image_denoise_metric import ImageDenoiseMetric
    from .image_instance_segmentation_metric import \
        ImageInstanceSegmentationCOCOMetric
    from .sequence_classification_metric import SequenceClassificationMetric
    from .text_generation_metric import TextGenerationMetric

else:
    _import_structure = {
        'base': ['Metric'],
        'builder': ['METRICS', 'build_metric', 'task_default_metrics'],
        'image_color_enhance_metric': ['ImageColorEnhanceMetric'],
        'image_denoise_metric': ['ImageDenoiseMetric'],
        'image_instance_segmentation_metric':
        ['ImageInstanceSegmentationCOCOMetric'],
        'sequence_classification_metric': ['SequenceClassificationMetric'],
        'text_generation_metric': ['TextGenerationMetric'],
    }

    import sys

    sys.modules[__name__] = LazyImportModule(
        __name__,
        globals()['__file__'],
        _import_structure,
        module_spec=__spec__,
        extra_objects={},
    )