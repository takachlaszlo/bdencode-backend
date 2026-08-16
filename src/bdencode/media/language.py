"""Language metadata with explicit provenance and confidence.

Blu-ray language tags are authored metadata, not ground truth.  This module keeps
the declaration, content based inference and a possible operator override apart so
the API never has to pretend an uncertain language is certain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Iterable, Mapping


class LanguageSource(StrEnum):
    MPLS = "mpls"
    CLPI = "clpi"
    PMT = "pmt"
    AUDIO_LID = "audio_lid"
    SUBTITLE_OCR = "subtitle_ocr"
    MANUAL = "manual"


class LanguageStatus(StrEnum):
    DECLARED = "declared"
    INFERRED = "inferred"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    OVERRIDDEN = "overridden"


# ISO 639-2/B aliases which commonly occur on authored discs.  Matroska should
# receive the terminological form (639-2/T) and its BCP-47 equivalent.
_BIBLIOGRAPHIC_ALIASES: dict[str, str] = {
    "alb": "sqi",
    "arm": "hye",
    "baq": "eus",
    "bur": "mya",
    "chi": "zho",
    "cze": "ces",
    "dut": "nld",
    "fre": "fra",
    "geo": "kat",
    "ger": "deu",
    "gre": "ell",
    "ice": "isl",
    "mac": "mkd",
    "mao": "mri",
    "may": "msa",
    "per": "fas",
    "rum": "ron",
    "slo": "slk",
    "tib": "bod",
    "wel": "cym",
}


# Deliberately finite: accepting every syntactically valid three-letter value
# would also accept typos.  It covers common optical-disc languages plus the
# special ISO codes.  The raw value is retained when validation fails.
_ISO639_2T_TO_BCP47: dict[str, str | None] = {
    "ara": "ar",
    "ben": "bn",
    "bod": "bo",
    "bos": "bs",
    "bul": "bg",
    "cat": "ca",
    "ces": "cs",
    "cym": "cy",
    "dan": "da",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "est": "et",
    "eus": "eu",
    "fas": "fa",
    "fin": "fi",
    "fra": "fr",
    "gle": "ga",
    "heb": "he",
    "hin": "hi",
    "hrv": "hr",
    "hun": "hu",
    "hye": "hy",
    "ind": "id",
    "isl": "is",
    "ita": "it",
    "jpn": "ja",
    "kat": "ka",
    "kaz": "kk",
    "kor": "ko",
    "lav": "lv",
    "lit": "lt",
    "mal": "ml",
    "mkd": "mk",
    "mon": "mn",
    "mri": "mi",
    "msa": "ms",
    "mya": "my",
    "nld": "nl",
    "nor": "no",
    "pan": "pa",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "rus": "ru",
    "sin": "si",
    "slk": "sk",
    "slv": "sl",
    "spa": "es",
    "sqi": "sq",
    "srp": "sr",
    "swe": "sv",
    "tam": "ta",
    "tel": "te",
    "tha": "th",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "vie": "vi",
    "zho": "zh",
    # Matroska/BCP-47 can carry the macrolanguage varieties that matter for
    # original-track policy even though they are ISO 639-3 rather than 639-2.
    "cmn": "cmn",
    "yue": "yue",
    "mul": "mul",
    "zxx": "zxx",
}

_INVALID_CODES = {"", "???", "und", "unk", "null", "none", "---"}
_CODE_RE = re.compile(r"^[a-z]{3}$")


def normalize_iso639_2(value: str | None) -> str | None:
    """Return a validated ISO 639-2/T code, otherwise ``None``.

    NUL-padded values from binary structures are accepted; unknown and malformed
    values deliberately remain unknown rather than becoming English.
    """

    if value is None:
        return None
    code = value.replace("\x00", "").strip().lower()
    if code in _INVALID_CODES or not _CODE_RE.fullmatch(code):
        return None
    code = _BIBLIOGRAPHIC_ALIASES.get(code, code)
    return code if code in _ISO639_2T_TO_BCP47 else None


def iso639_2_to_bcp47(value: str | None) -> str | None:
    code = normalize_iso639_2(value)
    return _ISO639_2T_TO_BCP47.get(code) if code else None


@dataclass(frozen=True, slots=True)
class LanguageEvidence:
    source: LanguageSource
    raw_code: str | None
    normalized_code: str | None = None
    confidence: float = 1.0
    detail: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("language evidence confidence must be between 0 and 1")
        expected = normalize_iso639_2(self.raw_code)
        if self.normalized_code is None:
            object.__setattr__(self, "normalized_code", expected)
        elif normalize_iso639_2(self.normalized_code) != self.normalized_code:
            raise ValueError("normalized_code must be a supported ISO 639-2/T code")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "raw_code": self.raw_code,
            "normalized_code": self.normalized_code,
            "confidence": self.confidence,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LanguageDecision:
    iso639_2t: str | None
    bcp47: str | None
    status: LanguageStatus
    confidence: float
    evidence: tuple[LanguageEvidence, ...] = field(default_factory=tuple)
    needs_review: bool = False
    overridden_by: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "iso639_2t": self.iso639_2t,
            "bcp47": self.bcp47,
            "status": self.status.value,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "overridden_by": self.overridden_by,
            "evidence": [item.to_dict() for item in self.evidence],
        }


class LanguageResolver:
    """Resolve declarations and inference without discarding disagreements."""

    _DECLARED_WEIGHTS: Mapping[LanguageSource, float] = {
        LanguageSource.MPLS: 0.92,
        LanguageSource.CLPI: 0.82,
        LanguageSource.PMT: 0.68,
    }

    def resolve(
        self,
        *,
        mpls: str | None = None,
        clpi: str | None = None,
        pmt: str | None = None,
        audio_lid: str | None = None,
        audio_confidence: float = 0.0,
        subtitle_ocr: str | None = None,
        subtitle_confidence: float = 0.0,
        override: str | None = None,
        overridden_by: str | None = None,
    ) -> LanguageDecision:
        evidence = self._evidence(
            mpls=mpls,
            clpi=clpi,
            pmt=pmt,
            audio_lid=audio_lid,
            audio_confidence=audio_confidence,
            subtitle_ocr=subtitle_ocr,
            subtitle_confidence=subtitle_confidence,
        )

        if override is not None:
            normalized = normalize_iso639_2(override)
            if normalized is None:
                raise ValueError(
                    "manual language override is not a supported ISO 639-2 code"
                )
            manual = LanguageEvidence(LanguageSource.MANUAL, override, normalized, 1.0)
            return self._decision(
                normalized,
                LanguageStatus.OVERRIDDEN,
                1.0,
                (*evidence, manual),
                needs_review=False,
                overridden_by=overridden_by,
            )

        valid = [item for item in evidence if item.normalized_code]
        if not valid:
            return self._decision(
                None, LanguageStatus.UNKNOWN, 0.0, evidence, needs_review=True
            )

        declared = [item for item in valid if item.source in self._DECLARED_WEIGHTS]
        content = [
            item
            for item in valid
            if item.source in {LanguageSource.AUDIO_LID, LanguageSource.SUBTITLE_OCR}
        ]
        mpls_code = self._code_for(valid, LanguageSource.MPLS)
        clpi_code = self._code_for(valid, LanguageSource.CLPI)

        declared_codes = {item.normalized_code for item in declared}
        strong_content = [item for item in content if item.confidence >= 0.85]
        # Authored MPLS/CLPI/PMT tags often repeat the same wrong language.  A
        # strong content result must therefore be allowed to contradict even a
        # unanimous declaration instead of being silently discarded.
        if len(declared_codes) == 1 and strong_content:
            declared_code = next(iter(declared_codes))
            disagreeing = [
                item for item in strong_content if item.normalized_code != declared_code
            ]
            if disagreeing:
                selected = max(disagreeing, key=lambda item: item.confidence)
                return self._decision(
                    selected.normalized_code,
                    LanguageStatus.CONFLICT,
                    selected.confidence,
                    evidence,
                    needs_review=True,
                )
        if mpls_code and clpi_code and mpls_code == clpi_code:
            confirmations = sum(item.normalized_code == mpls_code for item in valid)
            confidence = min(0.99, 0.96 + max(0, confirmations - 2) * 0.01)
            return self._decision(
                mpls_code, LanguageStatus.DECLARED, confidence, evidence
            )
        if len(declared_codes) > 1:
            # A strong content result can choose a likely value, but the authored
            # contradiction remains visible and requires review.
            corroborated = [
                item for item in strong_content if item.normalized_code in declared_codes
            ]
            selected = max(corroborated, key=lambda item: item.confidence, default=None)
            return self._decision(
                selected.normalized_code if selected else None,
                LanguageStatus.CONFLICT,
                selected.confidence if selected else 0.0,
                evidence,
                needs_review=True,
            )

        if declared:
            selected = max(
                declared, key=lambda item: self._DECLARED_WEIGHTS[item.source]
            )
            confirmations = [
                item
                for item in valid
                if item is not selected
                and item.normalized_code == selected.normalized_code
            ]
            confidence = self._DECLARED_WEIGHTS[selected.source]
            if confirmations:
                confidence = min(0.97, confidence + 0.04)
            return self._decision(
                selected.normalized_code,
                LanguageStatus.DECLARED,
                confidence,
                evidence,
                needs_review=False,
            )

        inferred = max(content, key=lambda item: item.confidence, default=None)
        if inferred and inferred.confidence >= 0.75:
            same = [
                item
                for item in content
                if item.normalized_code == inferred.normalized_code
            ]
            confidence = inferred.confidence
            if len(same) > 1:
                confidence = min(0.98, confidence + 0.04)
            return self._decision(
                inferred.normalized_code,
                LanguageStatus.INFERRED,
                confidence,
                evidence,
                needs_review=confidence < 0.90,
            )

        return self._decision(
            None, LanguageStatus.UNKNOWN, 0.0, evidence, needs_review=True
        )

    @staticmethod
    def _code_for(
        evidence: Iterable[LanguageEvidence], source: LanguageSource
    ) -> str | None:
        return next(
            (item.normalized_code for item in evidence if item.source == source), None
        )

    @staticmethod
    def _decision(
        code: str | None,
        status: LanguageStatus,
        confidence: float,
        evidence: Iterable[LanguageEvidence],
        *,
        needs_review: bool = False,
        overridden_by: str | None = None,
    ) -> LanguageDecision:
        return LanguageDecision(
            iso639_2t=code,
            bcp47=iso639_2_to_bcp47(code),
            status=status,
            confidence=round(confidence, 4),
            evidence=tuple(evidence),
            needs_review=needs_review,
            overridden_by=overridden_by,
        )

    @staticmethod
    def _evidence(**values: object) -> tuple[LanguageEvidence, ...]:
        result: list[LanguageEvidence] = []
        pairs = (
            (LanguageSource.MPLS, values["mpls"], 1.0),
            (LanguageSource.CLPI, values["clpi"], 1.0),
            (LanguageSource.PMT, values["pmt"], 1.0),
            (LanguageSource.AUDIO_LID, values["audio_lid"], values["audio_confidence"]),
            (
                LanguageSource.SUBTITLE_OCR,
                values["subtitle_ocr"],
                values["subtitle_confidence"],
            ),
        )
        for source, raw, confidence in pairs:
            if raw is not None:
                result.append(
                    LanguageEvidence(source, str(raw), confidence=float(confidence))
                )
        return tuple(result)
