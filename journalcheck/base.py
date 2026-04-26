from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from .models import SubmissionStatus


class SiteChecker(ABC):
    site_name: str

    @abstractmethod
    def check(self) -> Sequence[SubmissionStatus]:
        raise NotImplementedError
