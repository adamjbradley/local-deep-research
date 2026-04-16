"""JSON export service for structured research data.

Serializes the canonical structured_data dict as pretty-printed JSON.
"""

import json
from typing import Optional

from loguru import logger

from .base import BaseExporter, ExportOptions, ExportResult
from .registry import ExporterRegistry


@ExporterRegistry.register
class JSONExporter(BaseExporter):
    """Export structured research data as JSON."""

    @property
    def format_name(self) -> str:
        return "json"

    @property
    def file_extension(self) -> str:
        return ".json"

    @property
    def mimetype(self) -> str:
        return "application/json"

    def export(
        self,
        markdown_content: str,
        options: Optional[ExportOptions] = None,
    ) -> ExportResult:
        """Export structured data as pretty-printed JSON.

        Requires structured_data in options.custom_options.

        Raises:
            ValueError: If no structured data is provided.
        """
        options = options or ExportOptions()
        structured_data = (options.custom_options or {}).get("structured_data")

        if not structured_data:
            raise ValueError(
                "JSON export requires structured research data. "
                "Use the structured research mode to generate exportable data."
            )

        try:
            content = json.dumps(
                structured_data,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
            content_bytes = content.encode("utf-8")
            filename = self._generate_safe_filename(options.title)

            logger.info(
                f"Generated JSON export: {len(content_bytes)} bytes"
            )

            return ExportResult(
                content=content_bytes,
                filename=filename,
                mimetype=self.mimetype,
            )

        except ValueError:
            raise
        except Exception:
            logger.exception("Error generating JSON export")
            raise
